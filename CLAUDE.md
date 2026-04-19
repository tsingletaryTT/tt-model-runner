# tt-model-runner-gui

GTK4 + Textual app for managing tt-inference-server model deployments.
Pick a model, pick hardware, launch a Docker-backed inference server,
watch it come up, run benchmarks, and test tool calls.

Out of scope: prompt submission, completions, evals, multi-host.

## Entrypoints

- `./run`        — GTK4 GUI
- `./run --tui`  — Textual TUI (Plan 2)

## Architecture: AppController + thin views

`app/controller.py` owns all business logic:
- State machine (IDLE → LAUNCHING → PULLING_IMAGE → LOADING → READY, ERROR, STOPPING)
- ServerManager lifecycle (launch / stop / tail logs)
- HealthWorker (polls /v1/models or /tt-liveness)
- TimingStore (progress estimates)
- BenchmarkRunner (Plan 2)
- ToolClient (Plan 2)

Views (GTK `MainWindow`, Textual `TuiApp`) are thin:
- Register `on_*` callbacks on the controller
- Call controller public methods (`launch`, `stop`, `load_repo`, etc.)
- Never reach into controller internals

## Thread dispatch — the key seam

`AppController.__init__` accepts `dispatch_fn(fn, *args)` which schedules
`fn(*args)` on the UI event loop:

- GUI:  `dispatch_fn = GLib.idle_add`
- TUI:  `dispatch_fn = textual_app.call_from_thread`
- Tests: `dispatch_fn = lambda fn, *a: fn(*a)` (synchronous)

All `on_*` callbacks are posted through `dispatch_fn` — never called directly
from background threads. GTK/Textual widgets are only touched on their own
event loop.

Background timers in AppController use `threading.Timer` (not GLib.timeout_add)
so the controller has no GTK dependency.

## State machine

    IDLE ──► LAUNCHING ──► PULLING_IMAGE ──► LOADING ──► READY
      ▲                                         │           │
      └──────────── STOPPING ◄──────────────────┘           │
      ▲                                                      │
      └──────────── ERROR ◄─────────────────────────────────┘

## Adding a new feature to both UIs

1. Add `on_<feature>` callback to `AppController` (in `controller.py`)
2. Add `on_<feature>` to `ViewContract` in `tests/test_controller_contract.py`
3. Run tests — contract tests fail on both stubs
4. Implement `on_<feature>` in `GtkViewStub`, `TuiViewStub`, `MainWindow`, and TUI widget
5. Tests pass

## File layout

    app/
      controller.py      — AppController, ToolRoundTrip, BenchResult dataclasses
      tool_client.py     — OpenAI tool-call HTTP client (Plan 2)
      benchmark_runner.py — wraps run.py --workflow benchmarks (Plan 2)
      tui/               — Textual TUI (Plan 2)
      main.py            — GTK App entry + CSS; injects GLib.idle_add dispatch
      tui_main.py        — TUI entry (Plan 2)
      main_window.py     — Thin GTK view; Sidebar and MainPanel widgets unchanged
      config_panel.py    — ConfigPanel GTK widget (mostly unchanged)
      server_manager.py  — Launches run.py, tails log; no GLib dependency
      health_worker.py   — Polls health endpoint; no GLib dependency
      worker.py          — Dispatch shim (backward compat); set_dispatch() for GTK
      model_catalog.py   — Parses model_spec.json
      device_detector.py — Runs tt-smi -s
      timing_store.py    — Persistent load time estimates
      launch_options.py  — LaunchOptions dataclass + presets
      profiles.py        — Named launch profiles
      hf_cache.py        — HF model cache scanning
      ghcr_resolver.py   — Docker tag resolution
      docker_images.py   — Local Docker image listing
      app_settings.py    — ~/.config/tt-runner-gui/settings.json
    tests/
      test_controller_contract.py  — ViewContract ABC, GtkViewStub, TuiViewStub
      test_controller.py           — AppController unit tests (NullDispatch)
      test_tool_client.py          — Tool call HTTP round-trip tests (Plan 2)
      test_benchmark_runner.py     — Metric parsing + pass/fail tests (Plan 2)
      (existing tests unchanged)

## Running tests

```bash
cd /path/to/tt-model-runner-gui
PYTHONPATH=app pytest tests/ -v
```

## Benchmark integration

`benchmark_runner.py` wraps `run.py --workflow benchmarks` in the configured
tt-inference-server repo. Results JSON files match pattern
`benchmark_*_isl-*_osl-*_maxcon-*.json`. Parsed fields: mean_ttft_ms,
mean_tps, tps_decode_throughput, mean_e2el_ms, request_throughput.
Pass/fail evaluated against model_spec.json perf_reference targets.
