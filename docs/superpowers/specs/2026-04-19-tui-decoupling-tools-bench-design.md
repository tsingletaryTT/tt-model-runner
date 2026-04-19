# TUI, Decoupling, Tool Calling & Benchmarks — Design Spec
**Date:** 2026-04-19
**Status:** Approved

---

## Overview

Four tightly-related goals delivered together:

1. **Decouple UI from business logic** — extract `AppController` from `main_window.py` so the state machine and all domain logic live in a pure-Python class with no UI dependency.
2. **Add a TUI** — a Textual-based terminal interface that is feature-for-feature equivalent to the GTK GUI, sharing the same `AppController`.
3. **Add interactive tool calling** — when the server is READY, both UIs expose a tool-call tester: define tools (JSON schema), send a prompt, see the full multi-turn round-trip.
4. **Add benchmark integration** — both UIs expose a Bench tab that wraps `tt-inference-server`'s existing `run.py --workflow benchmarks`, parses the JSON output, and evaluates results against `model_spec.json` performance targets.

**Feature parity is enforced mechanically** via a `ViewContract` ABC: if `AppController` emits a new callback, the contract test fails until both the GTK view and TUI view handle it.

---

## Architectural Principle: AppController + Thin Views

```
┌──────────────────────────────────────────────────────┐
│                    AppController                     │
│  state machine · ServerManager · HealthWorker        │
│  TimingStore · ModelCatalog · BenchmarkRunner        │
│  ToolClient · dispatch_fn (injected at startup)      │
└───────────────┬──────────────────────────────────────┘
                │  callbacks: on_state_changed, on_log_line,
                │  on_progress, on_substage, on_catalog_loaded,
                │  on_cache_scanned, on_bench_result, on_tool_result
        ┌───────┴───────┐
        │               │
  ┌─────▼─────┐   ┌─────▼─────┐
  │  GTK GUI  │   │  Textual  │
  │           │   │    TUI    │
  └───────────┘   └───────────┘
```

### Thread dispatch — the key seam

`AppController.__init__` accepts a `dispatch_fn: Callable[[Callable, ...], None]` with signature `dispatch_fn(fn, *args)` that schedules `fn(*args)` on the UI event loop. Defaults to `lambda fn, *a: fn(*a)` (synchronous, for tests). Each UI provides its own at startup:

- **GUI:** `dispatch_fn = GLib.idle_add` (posts to GTK main loop)
- **TUI:** `dispatch_fn = textual_app.call_from_thread` (posts to Textual event loop)

`worker.py` is updated to delegate to the controller's dispatch function. The `GLib` import is removed from `server_manager.py`, `worker.py`, and all non-GUI modules.

**Tour card advancement** is owned by `AppController` — it starts a background timer on LOADING entry (12s interval) and emits updated `on_substage` payloads as the card index advances. Views are passive recipients.

---

## File Structure

```
app/
  controller.py             # NEW — AppController: state machine + all domain logic
  tool_client.py            # NEW — OpenAI-compatible multi-turn tool call client
  benchmark_runner.py       # NEW — wraps run.py --workflow benchmarks, parses JSON output
  tui/
    __init__.py
    app.py                  # Textual Application; registers call_from_thread dispatch
    screens.py              # MainScreen: hybrid rail + tab switcher
    widgets/
      __init__.py
      model_rail.py         # Collapsible left rail (model tree, device, port, launch)
      config_pane.py        # Config tab (mirrors ConfigPanel)
      log_pane.py           # Logs tab (filtered log view + stepper + progress + tour)
      tool_pane.py          # Tools tab (tool definition + prompt + round-trip display)
      bench_pane.py         # Bench tab (run config + live output + results table)
  main_window.py            # REFACTORED — thin GTK view over AppController
  config_panel.py           # REFACTORED — thin GTK view over AppController
  main.py                   # GUI entry — registers GLib.idle_add as dispatch_fn
  tui_main.py               # NEW — TUI entry — registers Textual dispatch_fn
  # Unchanged: model_catalog.py, device_detector.py, server_manager.py,
  #            health_worker.py, timing_store.py, launch_options.py,
  #            profiles.py, hf_cache.py, ghcr_resolver.py, docker_images.py,
  #            app_settings.py, worker.py (updated dispatch only)
run                          # UPDATED — adds --tui flag
tests/
  test_controller.py         # NEW — AppController unit tests, NullDispatch, no UI
  test_tool_client.py        # NEW — tool round-trip tests with mock HTTP
  test_benchmark_runner.py   # NEW — metric parsing + pass/fail against reference targets
  test_controller_contract.py # NEW — ViewContract ABC enforces feature parity
  # Existing tests unchanged
CLAUDE.md                    # NEW — project documentation
```

---

## AppController Interface

```python
class AppController:
    # Injected at construction
    dispatch_fn: Callable  # posts fn(*args) to the UI event loop

    # State (read-only from views)
    state: ServerState
    current_entry: Optional[ModelEntry]
    catalog: Optional[ModelCatalog]

    # Callbacks — views register these after construction
    on_state_changed:   Callable[[ServerState, str], None]
    on_log_line:        Callable[[str], None]
    on_progress:        Callable[[float, str], None]
    on_substage:        Callable[[str, str, str, str], None]  # stepper, tour_left, tour_right, dots
    on_catalog_loaded:  Callable[[ModelCatalog, List[str]], None]
    on_cache_scanned:   Callable[[ModelCacheInfo], None]
    on_bench_progress:  Callable[[str], None]   # live log line from benchmark run
    on_bench_result:    Callable[[BenchResult], None]
    on_tool_result:     Callable[[ToolRoundTrip], None]

    # Methods called by views
    def load_repo(path: Path) -> None
    def select_model(entry: ModelEntry) -> None
    def launch(entry: ModelEntry, port: str, options: LaunchOptions) -> None
    def stop() -> None
    def run_benchmark(mode: str, concurrency_sweeps: bool, percentile_report: bool) -> None
    def send_tool_call(tools: list, prompt: str) -> None
    def get_options() -> LaunchOptions
```

All callbacks are dispatched via `dispatch_fn` — never called directly from background threads.

**New dataclasses (defined in `controller.py`):**
```python
@dataclass
class ToolRoundTrip:
    step: str          # "call" | "result" | "final"
    name: str          # tool name (for "call" step)
    arguments: str     # JSON string (for "call" step)
    content: str       # result or final text

@dataclass
class BenchResult:
    model_name: str
    device: str
    timestamp: str
    isl: int; osl: int; concurrency: int
    mean_ttft_ms: float; p95_ttft_ms: Optional[float]
    mean_tps: float; tps_decode: float
    mean_e2el_ms: float; request_throughput: float
    tier_pass: str     # "PASS" | "BELOW_TARGET" | "FAIL"
```

**New dependencies:** `httpx` (tool client HTTP), `respx` (test mock for httpx), `textual` (TUI framework). Add to `requirements-dev.txt` and a new `requirements.txt`.

---

## New Feature: Tool Calling

### Core (`tool_client.py`)

Pure-Python HTTP client using `httpx` (new dependency). Runs synchronously inside a background thread via `threading.Thread` (consistent with the rest of the codebase — no asyncio). Sends an OpenAI-compatible `/v1/chat/completions` request with `tools=[...]` and `tool_choice="auto"`. Handles the multi-turn loop:

1. Send initial request with tools defined.
2. If response contains `tool_calls`, yield each call as a `ToolCall(name, arguments)`.
3. Caller injects tool results; client sends the follow-up request.
4. Yield `ToolResult(final_text)` when the assistant produces a non-tool response.

`AppController.send_tool_call()` drives the client, emits `on_tool_result` callbacks for each step (tool call, result injection, final reply) so both UIs can display the round-trip incrementally.

### UI (both)

**Tab: Tools** — visible when `state == READY`.

- Left pane: JSON tool definition editor (schema textarea), pre-filled with a sample `get_weather` tool.
- Right pane: prompt input + Send button.
- Output area: streaming round-trip display:
  ```
  → tool_call: get_weather({"city": "Austin"})
  ← tool result: {"temp": 82, "condition": "sunny"}
  → final: "It's 82°F and sunny in Austin."
  ```
- Tool use must have been enabled at launch time (config panel `tool_use_enabled`). If not, the tab shows a hint to re-launch with tool use enabled.

---

## New Feature: Benchmarks

### Core (`benchmark_runner.py`)

Wraps `tt-inference-server`'s existing `run.py --workflow benchmarks`.

**Discovery:** confirms `run.py` exists in the configured repo path. Reads `model_spec.json` `perf_reference` for the selected model to extract performance targets.

**Launch command:**
```
python3 run.py
  --workflow benchmarks
  --model <display_name>
  --tt-device <device>
  --limit-samples-mode <smoke-test|ci-nightly|ci-long>
  [--concurrency-sweeps]
  [--percentile-report]
```

**Output parsing:** watches for `benchmark_*_isl-*_osl-*_maxcon-*.json` files in `workflow_logs/` (same discovery pattern as log files). Parses:
- `mean_ttft_ms`, `p95_ttft_ms`
- `mean_tps` (user throughput), `tps_decode_throughput`
- `mean_e2el_ms`, `request_throughput`
- ISL, OSL, concurrency from filename

**Pass/fail evaluation** against `model_spec.json` `perf_reference[].targets`:
- `customer_functional` tier (10% tolerance): shown as ✗ FAIL if missed
- `functional` tier (50% tolerance): shown as ⚠ BELOW TARGET if missed
- `target` tier: shown as ✓ PASS if met

**Persistence:** results appended to `~/.config/tt-runner-gui/benchmarks.json` keyed by `(model_name, device_type, timestamp)`.

### UI (both)

**Tab: Bench** — visible when `state == READY`.

- Run config: mode selector (smoke-test / ci-nightly / ci-long), concurrency sweeps toggle, percentile report toggle.
- Run button; live log tail while running.
- Results table columns: ISL, OSL, Concurrency, TTFT ms, TPS User, TPS Decode, E2EL ms, Req/s, Pass/Fail.
- History section: previous runs for this model+device, newest first.

---

## TUI Layout (Textual)

### Hybrid collapsible left rail

```
┌─ Rail (collapsible) ─┬─ Main area ──────────────────────────────┐
│ [Model ▾]            │  [ Config ] [ Logs ] [ Tools ] [ Bench ] │
│  Llama-3.1-8B        │  ─────────────────────────────────────── │
│ [Device: N150 ▾]     │  <active tab content>                    │
│ [Port: 8000    ]     │                                          │
│ [▶  Launch     ]     │                                          │
│  ● IDLE              │                                          │
└──────────────────────┴──────────────────────────────────────────┘
```

- Rail width: 22 chars expanded, 4 chars collapsed (`[▶]`, `[●]`, etc.). Toggle with `Tab` or `[` key.
- State pill updates live in the rail regardless of which tab is active.
- Tab bar: Config (pre-launch), Logs (auto-switches on launch), Tools (READY only), Bench (READY only).
- Keyboard: `1-4` switch tabs; `l` = launch/stop; `q` = quit; `/` = filter logs.

### Loading state (Logs tab)

Mirrors GUI layout within the main area:
- Status bar at top: state pill + info text.
- Stepper row: `✓ Engine Init  ──  ● Device Mesh  ──  ○ Weights  ──  ○ ...`
- Progress bar (Textual `ProgressBar` widget).
- Tour panel: two-column static text (model info left, hardware education card right), auto-advances every 12s.
- Scrollable log below with level filter toggles (`D` `I` `W` `E` keys).

---

## Testing Strategy

### `test_controller.py`
Drives `AppController` with `dispatch_fn=lambda fn, *a: fn(*a)` (synchronous NullDispatch). Tests:
- State machine transitions via `LogParser.feed()` sequences.
- `on_progress` fraction accuracy during trace capture (10-step) and warmup.
- `on_substage` emission from mock log lines.
- Launch/stop lifecycle with mock `ServerManager`.

### `test_tool_client.py`
Uses `respx` (httpx mock) to simulate `/v1/chat/completions` responses. Tests:
- Single tool call round-trip yields `ToolCall` then `ToolResult`.
- Multi-step tool chains.
- Non-tool response (passthrough).
- HTTP error handling.

### `test_benchmark_runner.py`
Uses fixture JSON files mirroring `benchmark_*_isl-128_osl-128_maxcon-1_n-100.json`. Tests:
- Metric extraction (TTFT, TPS, E2EL).
- Pass/fail evaluation against reference targets at each tier.
- History persistence round-trip.

### `test_controller_contract.py`
Defines `ViewContract(ABC)` with `@abstractmethod` for every `on_*` callback `AppController` emits. Both `GtkViewStub` and `TuiViewStub` implement it. If a new callback is added to `AppController` but not to both stubs, the import of the stub fails and the test suite errors. This mechanically enforces feature parity.

---

## CLAUDE.md Contents

- Project purpose and scope (what it is, what it is not).
- File layout: core modules vs GUI vs TUI.
- State machine diagram (IDLE→LAUNCHING→PULLING_IMAGE→LOADING→READY, ERROR, STOPPING).
- Threading discipline: all UI updates via `dispatch_fn`; background threads must never touch widgets.
- AppController/view pattern: how to register callbacks, how to add a new feature to both UIs.
- Entrypoints: `./run` (GUI), `./run --tui` (TUI).
- How to run tests: `pytest tests/`.
- Benchmark workflow: how `benchmark_runner.py` discovers and wraps `tt-inference-server`.

---

## Out of Scope

- Prompt submission / completions playground (separate tool: tt-local-generator).
- Quality benchmarks (MMLU, HellaSwag) — separate tooling required.
- Multi-host deployments.
- Remote server management.
