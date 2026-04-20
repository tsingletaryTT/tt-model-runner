# tt-model-runner-gui

GTK4 + Textual app for managing tt-inference-server model deployments.
Pick a model, pick hardware, launch a Docker-backed inference server,
watch it come up, run benchmarks, and test tool calls.

Out of scope: prompt submission, completions, evals, multi-host.

## Entrypoints

- `./run`        — GTK4 GUI
- `./run --tui`  — Textual TUI (same AppController, different view)

## Architecture: AppController + thin views

Key modules:
- `app/controller.py` — AppController: state machine, all domain logic, no UI imports
- `app/tool_client.py` — Synchronous httpx multi-turn tool-call session
- `app/benchmark_runner.py` — Wraps `tt-inference-server/run.py --workflow benchmarks`
- `app/tui/` — Textual TUI package: app.py, widgets/

`app/controller.py` owns all business logic:
- State machine (IDLE → LAUNCHING → PULLING_IMAGE → LOADING → READY, ERROR, STOPPING)
- ServerManager lifecycle (launch / stop / tail logs)
- HealthWorker (polls /v1/models or /tt-liveness)
- TimingStore (progress estimates)
- BenchmarkRunner
- ToolClient

Views (GTK `MainWindow`, Textual `TuiApp`) are thin:
- Register `on_*` callbacks on the controller
- Call controller public methods (`launch`, `stop`, `load_repo`, etc.)
- Never reach into controller internals

## Threading discipline

All `on_*` callbacks dispatched via `AppController._emit` → `dispatch_fn`:
- GTK: `GLib.idle_add`
- TUI: `app.call_from_thread`
- Tests: sync lambda

Never call widget methods from background threads.

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
      ▲
      └──────────── DONE   ← dev-image script exited 0
      ▲
      └────────────── RUNNING ← dev-image script executing

Views treat RUNNING like LOADING (show logs, lock sidebar).
Views treat DONE like IDLE (unlock sidebar, keep logs).

## Adding a new feature to both UIs

1. Add `on_<feature>` callback to `AppController` (in `controller.py`)
2. Add `on_<feature>` to `ViewContract` in `tests/test_controller_contract.py`
3. Run tests — contract tests fail on both stubs
4. Implement `on_<feature>` in `GtkViewStub`, `TuiViewStub`, `MainWindow`, and TUI widget
5. Tests pass

## File layout

    app/
      controller.py      — AppController, ToolRoundTrip, BenchResult dataclasses
      tool_client.py     — Synchronous httpx multi-turn tool-call session
      benchmark_runner.py — wraps run.py --workflow benchmarks
      tui/               — Textual TUI package: app.py, widgets/ (ModelRail, LogPane, ConfigPane, ToolPane, BenchPane)
      main.py            — GTK App entry + CSS; injects GLib.idle_add dispatch
      tui_main.py        — TUI entry; injects call_from_thread dispatch
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
      compat_catalog.py  — Fetch/cache Tenstorrent compatibility.json (222 models, 24 h TTL)
      ad_facts.py        — Did-you-know card pool + model recommendation logic for AdUnit
      dev_image_launcher.py — Docker-based launcher for tt-forge/tt-metal models via tt-developer-image
    data/
      did-you-know.json  — 36 rotating educational cards shown in AdUnit
      model-classifications.json — Task/category tags for models
      model-descriptions.json    — Descriptions for model catalog entries
    tests/
      test_controller_contract.py  — ViewContract ABC, GtkViewStub, TuiViewStub
      test_controller.py           — AppController unit tests (NullDispatch)
      test_tool_client.py          — Tool call HTTP round-trip tests (Plan 2)
      test_benchmark_runner.py     — Metric parsing + pass/fail tests (Plan 2)
      (existing tests unchanged)

## TUI key bindings

| Key | Action |
|-----|--------|
| L | Launch / Stop server |
| Q | Quit |
| 1–4 | Switch to Config / Logs / Tools / Bench tab |
| [ | Toggle ModelRail sidebar |
| S | Star / unstar selected model |
| R | Reconnect to detected running server |
| Ctrl+R | Restart server (same model, no re-pull) |
| Ctrl+H | Refresh chip telemetry (tt-smi -s) |
| Ctrl+T (×2) | Hardware reset (tt-smi -r) — requires two presses within 5 s |
| Ctrl+U | Copy test curl command to clipboard (READY only) |
| Ctrl+F | Open log search (Esc to close, D/I/W/E to toggle levels) |

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
