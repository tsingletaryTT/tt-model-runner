# tt CLI modernization + tt-model-manager community models

Date: 2026-09-17
Status: approved, entering implementation planning

## Prompt

> Let's modernize this to work with the new `tt` and `tt-model` CLI and expand
> the source of truth to include tt-model-manager models using their serve
> commands when appropriate (versus just tt-inference-server).
>
> (though tt-inference-server has advanced in recent months to include a vllm
> plugin, consider making sure we're using the right pattern still)

## Research summary

Three background research passes (Glean + source reads against
`tenstorrent/tt-cli`, `tenstorrent/tt-model-manager`, `tenstorrent/vllm-tt-plugin`,
and `tenstorrent/tt-inference-server`) established:

- **`tt-model-manager`** (binary `tt-model`) packages and serves *community*
  model bundles distributed via Hugging Face Hub. It is fully self-contained —
  no Docker orchestration, no dependency on tt-inference-server. Key command:
  `tt-model serve <org>/<name> [--port N] [--profile NAME]`. Bundles declare a
  `kind` (`vllm` or `tt-dit-server` for non-token ASGI apps like diffusion) and
  an `arch`/`mesh`/`device_count`, not a `model_spec.json`-style `device_type`.
- **`tt` (tt-cli)** is a thin wrapper that merges tt-inference-server's
  `release_model_spec.json` with tt-model-manager community bundles
  (`tt model list [--all|--community]`) and dispatches `tt serve NAME` to
  whichever backend owns that name. This dispatch logic is itself beta
  (merged ~2026-09-14) and manages its **own** git+venv checkout of
  tt-inference-server — it does not reuse a user-configured repo path.
- Tenstorrent's own "single source of truth" unified manifest (Jira
  DEVSTACK-216/220) is a **backlog design goal, not shipped** — there is
  nothing to integrate against yet beyond what `tt model list` already merges.
- **`vllm-tt-plugin` is real but not yet what tt-inference-server uses.**
  As of tt-inference-server v0.19.x, `workflows/workflow_types.py`'s
  `InferenceEngine` enum is still exactly `VLLM | MEDIA | FORGE`, and vLLM
  images still clone Tenstorrent's (now-deprecated-for-new-work) vLLM fork
  with the plugin vendored inside, pinned per model. The standalone plugin
  path is explicitly framed upstream as experimental/optional.
- `run.py`'s CLI surface (`--workflow server`, `--docker-server`,
  `--tt-device`, `--override-docker-image`, `--no-auth`, `--service-port`) is
  unchanged. `model_spec.json`'s schema and `InferenceEngine` enum are
  unchanged. One new rule: `--dev-mode` combined with `--docker-server` now
  requires `--override-docker-image` (dev specs don't pin an image).
- The two bugs `app/server_manager.py`'s docker shim works around (missing
  `CACHE_ROOT` for vLLM containers; bare `--model` CMD args failing under
  `gosu exec` on new-format images) are **fixed in current upstream Dockerfiles**
  — the shim's existing new-format/old-format detection already handles both
  cases safely, so no change is required there.

## Decisions

1. **Keep the tt-inference-server launch path exactly as-is.** `ServerManager`
   keeps driving `run.py` directly against the user's configured repo
   checkout — the docker shim, GHCR resolution, and auto-remediation logic
   are confirmed still correct and are not touched by this project.
2. **Add tt-model-manager community bundles as a second model source**,
   launched through a new, separate launcher that shells out to
   `tt-model serve` — not a replacement for `run.py`, and not routed through
   `tt serve`'s own auto-dispatch (the app already knows which source an
   entry came from, so it can call the right backend directly).
3. **Discover community bundles via `tt model list --community`** (shelling
   the `tt` CLI), not by querying the HF-backed index directly — one
   dependency (the tt CLI toolchain), inherits whatever curation tt-cli
   applies, and avoids depending on an unconfirmed-stable index API.
4. **Community models get their own top-level "Community" section** in both
   sidebars, not merged into the existing model_type → family tree — keeps
   the curated catalog's UX unchanged and signals these are less vetted
   (mirroring tt-cli's own exclusion of community bundles from its default
   list, and Model Manager's "unsupported" framing).
5. Community entries merge into the existing `ModelCatalog`/`ModelEntry`
   (tagged by a new `source` field) rather than a separate catalog class, to
   keep existing filtering/tree-building code working unchanged. This can be
   split out later if it causes friction.

## Data model changes

`ModelEntry` (`app/model_catalog.py`) gains:

- `source: str` — `"inference_server"` (default, existing entries) or
  `"community"`.
- Existing fields are reused for community entries where the shape fits:
  - `model_id` / `model_name` ← the bundle id (`"org/name"`)
  - `hf_model_repo` ← the bundle's `weights` pointer (HF repo id)
  - `inference_engine` ← the bundle's `kind` (`vllm` / `tt-dit-server`) so the
    existing `LogParser` regexes apply unmodified (both kinds produce log
    patterns it already recognizes — vLLM startup lines, or ASGI/uvicorn
    startup for `tt-dit-server`)
  - `device_type` ← best-effort mapped from the bundle's `arch`/`mesh` onto
    the existing device-type taxonomy (e.g. `p150` → `P150`); unmappable
    values fall back to a new `"UNKNOWN"` bucket that is always shown —
    community bundles skip the strict hardware-compatibility gate that
    `get_compatible`/`get_blackhole_family` apply to inference-server entries
  - `status` ← new value `"COMMUNITY"`, driving a badge in the UI
  - `docker_image` ← empty (no docker image; bundles are self-contained)
  - `param_count` / `min_disk_gb` / `min_ram_gb` ← `None` (not exposed by the
    community catalog today)

A new `app/community_catalog.py` shells `tt model list --community` (JSON
output if the CLI supports it; a defensive text-output fallback otherwise —
implementation must probe both, since this is unconfirmed), parses results
into `ModelEntry(source="community", ...)`, and exposes a function to merge
those entries into an existing `ModelCatalog` (append to `_entries`, no new
catalog class). Mirrors `compat_catalog.py`'s shape (fetch → cache → parse)
but the fetch is a subprocess call, not HTTP — cache TTL should be short
(e.g. in-memory per app session, refreshed on demand) since community
listings are more mutable than the 24h-cached `compatibility.json`.

## Launcher & controller changes

New `app/tt_model_launcher.py::TtModelLauncher`, same public shape as
`DevImageLauncher` (`launch(config, on_log_line, on_state)`, `stop()`):

- `launch()` shells `tt-model serve <bundle_id> --port <port> [--profile <profile>]`
  as a foreground subprocess (simpler than `DevImageLauncher` — no Docker
  container to poll for liveness, `tt-model serve` owns the process directly)
  and tails its stdout through the **existing** `LogParser` for state
  transitions.
- Readiness is detected via the **existing** `HealthWorker` (already just
  polls `/v1/models` / `/tt-liveness` on the configured port — bundle-agnostic,
  no changes needed).
- `stop()` shells `tt-model stop <bundle_id>`.
- No GHCR resolution, no auto-remediation, no retry logic initially — errors
  surface as `ServerState.ERROR` with `tt-model serve`'s own stderr/exit code
  in the log. `tt-model`'s own `compare()` step already fatally rejects
  `arch` mismatches and warns on `device_count` mismatches; the launcher does
  not duplicate that pre-flight check.

`AppController` gets a new public method `launch_community(entry, port, ...)`
alongside `launch()` and `launch_dev_image()`, following the exact same
pattern: build a small `TtModelLaunchConfig`, call
`self._tt_model_launcher.launch(...)` wired to the existing
`_handle_log_line`/`_on_server_state` callbacks. Views branch on
`entry.source` to decide which controller method to call, mirroring how they
already branch for dev-image entries today.

Community catalog loading follows the `on_compat_catalog_loaded` precedent:
a new `on_community_catalog_loaded` callback fired from a background
`_community_load_async`, merging results into `self._catalog` and
re-emitting the tree. Added to `ViewContract` first (per the project's
"Adding a new feature to both UIs" workflow), implemented in
`GtkViewStub`/`TuiViewStub`/`MainWindow`/TUI widgets after contract tests
fail on both stubs.

## UI changes

Both `MainWindow`/`Sidebar` (GTK) and `ModelRail` (TUI) get a new top-level
"Community" section rendered after the existing model_type/family tree,
populated by filtering `catalog.all_entries()` on `source == "community"`
and grouped by bundle owner (the `org` in `org/name`) since community
bundles don't carry `model_type`/`family`.

Selecting a community entry shows a simplified config panel (bundle id,
port, optional profile) instead of the full `LaunchOptions` panel — community
bundles don't take vLLM tuning flags, docker image overrides, or the other
inference-server-specific knobs. Launch/Stop keybindings (TUI `L`) and
buttons (GTK) call `controller.launch_community(...)` when the selected
entry's `source` is `"community"`, the same branch shape already used for
dev-image entries.

## Error handling

- **`tt`/`tt-model` not installed**: checked via `shutil.which("tt-model")`
  (or `tt`) on first community-catalog load. If absent, the Community
  section stays empty with a one-line "install the `tt` CLI to see community
  models" hint — no hard dependency on `tt` for the app's core
  (inference-server) functionality, and no error surfaced as if it were a
  launch failure.
- **No auto-remediation for community launches (initially)** — deliberately
  simpler than `ServerManager`'s remediation, which exists because of
  specific, well-characterized tt-inference-server Docker failure modes.
  Community bundle failure modes aren't characterized yet; remediation can
  grow later the same way `ServerManager`'s did, once real failures are
  observed.
- `--dev-mode` + `--docker-server` requiring `--override-docker-image`: the
  config panel/`LaunchOptions` validation should reflect this rule so the
  existing (unrelated to this project's main scope, but confirmed during
  research) constraint doesn't silently produce a broken `run.py` invocation.

## Testing

- `tests/test_community_catalog.py` — unit tests for the `tt model list
  --community` output parser against fixture text/JSON (both formats must be
  handled since which one the CLI emits is unconfirmed).
- `tests/test_tt_model_launcher.py` — unit tests for log-line-driven state
  transitions and command construction with a stubbed subprocess, no real
  `tt-model` binary required, following `test_benchmark_runner.py`'s style.
- `tests/test_controller_contract.py` — extended with the new
  `on_community_catalog_loaded` entry; contract tests must fail on both
  stubs before implementation, per project convention.

## Out of scope

- Replacing `run.py`/Docker orchestration with `tt serve` for
  tt-inference-server models.
- Building against tt-inference-server's unified manifest (DEVSTACK-216) —
  not shipped.
- Adopting `vllm-tt-plugin` directly — tt-inference-server itself doesn't use
  it yet.
- Auto-remediation/retry logic for community bundle launch failures.
