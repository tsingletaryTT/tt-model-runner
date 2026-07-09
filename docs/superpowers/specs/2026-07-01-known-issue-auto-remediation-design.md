# Known-issue auto-remediation — design

**Date:** 2026-07-01
**Status:** Approved (design), pending implementation plan

## Motivation

Getting a model running on a given Tenstorrent card sometimes requires
non-obvious, hard-won workarounds that today only live in a person's head or a
Slack thread. The canonical example (Adam Housman, #tt-inference-server,
2026-07-01):

Running `run.py --model Llama-3.1-8B --device p100 --workflow server
--docker-server` loads the model and passes its health check, then **crashes
during startup warmup** (background trace capture) on the first 2048-token
prefill:

```
TT_THROW: Statically allocated circular buffers ... clash with L1 buffers
```

which kills the vLLM engine.

**Root cause:** tt-metal's `MAX_PREFILL_CHUNK_SIZES_DIV1024` table in
`model_config.py` has no entry for P100 (or P150/P300 — only
N150/N300/T3K/TG/P150x4). The fallback overflows P100's L1 SRAM at ≥2048-token
prefills. Tracked upstream as `tenstorrent/tt-metal#28835`.

**Manual workaround Adam had to discover:**
1. Add `MAX_PREFILL_CHUNK_SIZE=2` to `.env` **in the repo root** (this var is
   read from exactly `.env`, never from exported environment variables).
2. Cap `max_model_len` below 2048 at launch
   (`--vllm-override-args '{"max_model_len": 1024}'`). 2048 is a hard floor
   (`MIN_CHUNK_SIZE`), so this does **not** rescue requests that genuinely need
   a 2048-token prefill — it only avoids the crash for smaller ones.
3. Side-effect: because in v0.10.0 `HF_TOKEN`/`JWT_SECRET` are *also* only read
   from `.env` (not exports), those must be moved into `.env` too, or the
   launch fails once you start relying on `.env`.

**Goal:** let tt-model-runner recognize this class of device/model
incompatibility, apply the known fix itself, and relaunch — turning a
multi-hour debugging session into an automatic recovery.

## Decisions (locked during brainstorming)

1. **Autonomy: auto-diagnose + auto-retry.** The tool detects the crash,
   applies the known workaround, and relaunches automatically. The user watches
   it recover rather than clicking through fixes.
2. **Knowledge lives in a data-driven KB file** (`data/workarounds.json`).
   Adam's P100 case is entry #1. New workarounds are added as data, not code.
   The schema is deliberately compatible with a future remote-sync upgrade
   (à la `compat_catalog.py`), but this pass ships bundled-file only.
3. **Trigger: pre-flight + reactive, transparent.** At launch, a matching
   device+model pre-applies the fix with a logged banner explaining what/why
   and the lossy tradeoff. The reactive crash→retry path stays as a safety net
   for anything not caught pre-flight. Every applied change is logged and
   reversible.
4. **CLI-ready, headless core + a `doctor` command** (inspired by
   `tenstorrent/tt-local-generator`'s `tt-ctl`). The resolver and `apply_remedy`
   have zero UI dependencies so GTK, TUI, and any future headless CLI share one
   implementation. This pass ships one small read-only diagnostic entry point
   (`doctor`) that dry-runs the KB against a device+model and prints what it
   *would* do — mirroring `tt-ctl recover`'s "report findings, don't mutate"
   ethos. A full `tt-runner-ctl` view (launch/status/logs/mcp-config) is noted
   as a future step, not built here.

### Two defaulted calls (easy to flip)

- **(a) Reactive auto-relaunch is capped at exactly 1 attempt.** If the single
  retry also crashes, the tool stays in ERROR and surfaces the hint text + ref
  link the old way. This guarantees no relaunch loops.
- **(b) `.env` mutations auto-apply** (writing `MAX_PREFILL_CHUNK_SIZE`,
  relocating `HF_TOKEN`/`JWT_SECRET`). This is consistent with the chosen
  auto+transparent+undo model: the change is logged prominently and an undo is
  available. If we later decide `.env` rewrites are too invasive to do silently,
  a per-remedy `env_requires_confirm: true` flag can downgrade just that step to
  warn-only without touching the rest of the design.

## Architecture

A new pure-logic module + a bundled KB, consulted by the controller at two
points in the *existing* launch flow. No new UI framework coupling: the
resolver has zero GTK/Textual dependencies, matching the other
controller-adjacent modules (`device_detector`, `launch_options`, etc.).

```
data/workarounds.json          ← knowledge base (bundled)
app/workaround_resolver.py     ← pure matching logic + Workaround dataclass (no UI deps)
app/controller.py              ← consults resolver at pre-flight and on ERROR; owns apply_remedy
app/server_manager.py          ← gains _set_env_key (sibling of _scrub_env_key)
run / app/main.py              ← gains a --doctor headless dry-run entry (tt-ctl-inspired)
```

### Integration points in `controller.py`

- **Pre-flight:** inside `_preflight_and_launch` (currently ends at
  `controller.py:488` with `self._do_launch(...)`). Before launching, call
  `match_preflight(device, model, repo_version)`. For each applicable remedy,
  `apply_remedy(...)`, emit the banner + `on_remediation_applied`, then launch
  already-fixed.
- **Reactive:** the ERROR-hint scan at `controller.py:1026` and the
  `_transition(ServerState.ERROR)` at `controller.py:1099`. On an ERROR
  transition, run `match_symptom(line, device, model)` across log lines. On a
  hit not already applied this session, `apply_remedy`, emit banner, and
  relaunch once (`self._remediation_attempts` capped at 1).

## Component: `data/workarounds.json`

Array of workaround objects:

```jsonc
[{
  "id": "p100-llama31-8b-l1-prefill",
  "devices": ["P100", "P150", "P300"],      // matched against device_detector DeviceType names
  "models": ["Llama-3.1-8B*"],              // glob (fnmatch) against ModelEntry.display_name
  "symptom": "clash with L1 buffers",       // regex, tested against ERROR log lines
  "env": { "MAX_PREFILL_CHUNK_SIZE": "2" }, // written to repo-root .env
  "vllm": { "max_model_len": 1024 },        // merged into --vllm-override-args
  "also_move_to_env": ["HF_TOKEN", "JWT_SECRET"],  // v0.10.0 .env-only gotcha
  "auto": true,                             // true = may auto-apply; false = warn-only
  "tradeoff": "Requests needing a >1024-token prefill will fail — a hard P100 limit (MIN_CHUNK_SIZE=2048).",
  "ref": "tt-metal#28835",
  "applies_to_versions": "<=0.10.0"         // optional PEP440-ish constraint vs server repo tag
}]
```

Field semantics:

- `devices` — empty/absent means "any device".
- `models` — empty/absent means "any model"; otherwise any glob match qualifies.
- `symptom` — required for the reactive path; a remedy with no `symptom` is
  pre-flight-only.
- `env` — keys/values written verbatim into repo-root `.env`.
- `vllm` — merged into `LaunchOptions` overrides (reuses existing
  `max_model_len` → `max_num_batched_tokens` coupling in `launch_options.py`).
- `also_move_to_env` — env var names to copy from the process environment into
  `.env` if not already present there.
- `auto` — `false` downgrades the whole remedy to warn-only (log + one-click
  apply), even in reactive mode.
- `applies_to_versions` — optional; when the server repo tag can be resolved,
  skip remedies whose constraint doesn't hold. Unknown version → treat as
  applicable (fail open, since the crash still tells the truth reactively).

## Component: `app/workaround_resolver.py`

Pure logic, no UI imports. Loads and caches the bundled JSON.

```python
@dataclass
class Workaround:
    id: str
    devices: list[str]
    models: list[str]
    symptom: str | None
    env: dict[str, str]
    vllm: dict[str, object]
    also_move_to_env: list[str]
    auto: bool
    tradeoff: str
    ref: str
    applies_to_versions: str | None

def load_workarounds() -> list[Workaround]: ...

def match_preflight(device: str, model: str,
                    repo_version: str | None) -> list[Workaround]:
    """Remedies whose device+model (+version) match, regardless of symptom."""

def match_symptom(log_line: str, device: str,
                  model: str) -> Workaround | None:
    """First remedy whose device+model match AND whose symptom regex hits."""
```

Matching rule: `device in devices` (or devices empty) **and** `model` matches
some glob in `models` (or models empty) **and** — for `match_preflight` — the
version constraint holds; for `match_symptom` — the `symptom` regex matches the
line.

## Component: applying a remedy

`apply_remedy(remedy, options, repo_path) -> AppliedRemedy`, implemented as a
private `AppController` method. It sits in the controller because it already
imports both `LaunchOptions` and `server_manager` (for `_set_env_key`), so no
new cross-module dependency is introduced and the resolver stays pure matching
logic:

1. Merge `remedy.vllm` into `LaunchOptions` (e.g. set `options.max_model_len`),
   letting `build_launch_args` do the rest.
2. Write each `remedy.env` key into repo-root `.env` via a new
   `_set_env_key(path, key, value)` helper in `server_manager.py`
   (idempotent set-or-replace, sibling to the existing `_scrub_env_key`).
3. For each name in `also_move_to_env`, if present in the process environment
   but absent from `.env`, copy it into `.env`.
4. Return an `AppliedRemedy` record (which `.env` keys were written, which
   overrides were set) so an **undo** can scrub those keys and clear the
   overrides. Stored on the controller as `self._applied_remedy`.

## Reactive retry loop

- On ERROR transition, scan log lines with `match_symptom`.
- If a remedy hits and `self._remediation_attempts < 1` and it hasn't already
  been applied this session:
  - `apply_remedy`, emit banner + `on_remediation_applied`, increment
    `self._remediation_attempts`, and relaunch (same model/port/options).
- If the relaunch also crashes: stay in ERROR, surface the existing hint text +
  `ref` link. No further auto-retry (cap = 1).
- Reuses the existing `_emitted_error_hints` dedup discipline so a remedy banner
  isn't emitted repeatedly for the same run.

## UX / transparency

Both paths emit a structured banner in the project's left-bar box style (no
right border, per house terminal-output rule):

```
╔══════════════════════════════════════════
║ PRE-FLIGHT: P100 + Llama-3.1-8B
║ matches known issue tt-metal#28835
║ Applying: MAX_PREFILL_CHUNK_SIZE=2, max_model_len=1024
║ ⚠ requests needing >1024-token prefill will fail — known P100 limit
║ [config logged · undo available]
╚══════════════════════════════════════════
```

A new `on_remediation_applied(remedy)` controller callback is added to
`ViewContract` (per the "adding a feature to both UIs" playbook in CLAUDE.md).
GTK/TUI may optionally render an "auto-tuned · undo" affordance; at minimum both
log the banner. `auto: false` remedies log a warning + one-click apply instead
of self-applying.

## Component: `doctor` — headless dry-run diagnostic

Inspired by `tt-ctl recover`/`status`: a read-only command that reports what the
tool *would* do, mutating nothing. It gives "figure out and try Adam's fixes" a
scriptable, GUI-free entry point (usable by a person, CI, or an agent).

- **Surface:** a `--doctor` flag on the existing `./run` entry point (cheapest —
  no new binary; reuses the same arg plumbing as `--tui`). It resolves the
  device from `device_detector` (or a `--device` override) and the model from a
  `--model` override, calls `workaround_resolver.match_preflight(...)`, and
  prints each matching workaround: id, `ref`, the `env`/`vllm` it would apply,
  and the `tradeoff`. Exits non-zero when matches exist (so scripts/CI can gate
  on "known issue present"), zero when clean.
- **Output discipline (borrowed from tt-ctl):** colour only when
  `stdout.isatty()`; machine-readable lines on stdout; guidance/notes on stderr.
  A `--json` flag emits the matched workarounds as JSON for programmatic use.
- **No mutation:** `doctor` never writes `.env` or launches. It only reports.
  Applying still happens through the normal launch (pre-flight) or reactive path.

This keeps the headless surface tiny while proving out the shared core; a fuller
`tt-runner-ctl` (launch/status/logs, and a `mcp-config` command like tt-ctl's so
Claude Code can drive the tool) is a documented future step, out of scope here.

## Testing

- `tests/test_workaround_resolver.py`
  - glob/device/version/symptom matching, including near-misses (P100 vs N150;
    right device, wrong model; symptom regex miss).
  - version constraint: applies / skips / fail-open on unknown.
- Controller tests (NullDispatch, in `tests/test_controller.py`)
  - pre-flight applies the remedy (env write + override) before launch.
  - a simulated crash log line triggers exactly one relaunch.
  - a second crash after retry does **not** loop (cap = 1).
  - undo scrubs the written `.env` keys and clears overrides.
- `.env` round-trip test for `_set_env_key` (set / overwrite / then scrub).
- Extend `ViewContract` + `GtkViewStub` + `TuiViewStub` for
  `on_remediation_applied` (contract tests fail until both stubs implement it).
- `doctor` tests: matching device+model prints the remedy and exits non-zero;
  a clean combo exits zero; `--json` emits valid JSON; and `doctor` writes no
  `.env` / launches nothing (mutation-free assertion).

## Scope guardrails (YAGNI — explicitly out of this pass)

- Remote KB sync (schema is ready for it; bundled file only for now).
- Multi-step / interactive remediations (a remedy is one atomic set of
  env + vllm changes).
- Remedies that require rebuilding tt-metal or otherwise can't be expressed as
  env/override tweaks.
- A full `tt-runner-ctl` CLI view (launch/status/logs) and an MCP `mcp-config`
  command — the resolver is built headless-ready and `doctor` proves the
  pattern, but the broader CLI surface is a separate future effort.
- Prompt submission / evals / anything already out of the tool's scope.

## Seed entry

`data/workarounds.json` ships with exactly the Adam P100 case above as entry
`p100-llama31-8b-l1-prefill`, which fully expresses his manual workaround and
serves as the worked example for adding future entries.
