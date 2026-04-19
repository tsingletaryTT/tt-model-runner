# AppController Foundation — Implementation Plan 1 of 2

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract `AppController` from `main_window.py`, abstract the GLib dispatch seam, and refactor the GTK window into a thin view — making the app ready for the TUI and new features in Plan 2.

**Architecture:** A new `AppController` class owns the state machine, `ServerManager`, `HealthWorker`, timers, and all business logic. It accepts a `dispatch_fn(fn, *args)` at construction that posts callbacks to the correct event loop. Both GTK and Textual replace the single GLib coupling point by injecting their own dispatch function. Views register `on_*` callbacks and call public methods; they never reach into internals. Feature parity is enforced by `ViewContract` ABC — if a callback is added to the controller, the contract test fails until both GUI and TUI stubs implement it.

**Tech Stack:** Python 3.12, GTK4 (gi), threading.Timer (replaces GLib.timeout_add in core), pytest, textual (dep added now for Plan 2), httpx + respx (added now for Plan 2 tool client tests).

**Spec:** `docs/superpowers/specs/2026-04-19-tui-decoupling-tools-bench-design.md`

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `app/controller.py` | **Create** | AppController — state machine, timers, all domain logic |
| `app/worker.py` | **Modify** | Remove GLib; expose `set_dispatch()` module fn for backward compat |
| `app/health_worker.py` | **Modify** | Remove `from worker import idle_add_once`; accept `dispatch_fn` arg |
| `app/server_manager.py` | **Modify** | Remove `from worker import idle_add_once`; call callbacks directly |
| `app/main_window.py` | **Modify** | `MainWindow` becomes thin view; `Sidebar` and `MainPanel` unchanged |
| `app/main.py` | **Modify** | Inject `GLib.idle_add` as dispatch_fn; construct `AppController` |
| `app/tui_main.py` | **Create** | Stub TUI entry (wired fully in Plan 2) |
| `requirements.txt` | **Create** | Runtime deps: requests, httpx, textual |
| `requirements-dev.txt` | **Modify** | Add respx |
| `CLAUDE.md` | **Create** | Project documentation |
| `tests/test_controller_contract.py` | **Create** | ViewContract ABC + GtkViewStub + TuiViewStub |
| `tests/test_controller.py` | **Create** | AppController unit tests via NullDispatch |

---

## Task 1: Add requirements files

**Files:**
- Create: `requirements.txt`
- Modify: `requirements-dev.txt`

- [ ] **Step 1: Create requirements.txt**

```
requests>=2.31
httpx>=0.27
textual>=0.61
```

- [ ] **Step 2: Update requirements-dev.txt**

Replace the entire file with:
```
pytest
respx>=0.21
```

- [ ] **Step 3: Install new deps**

```bash
pip install httpx textual respx
```

Expected: installs without errors.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt requirements-dev.txt
git commit -m "chore: add runtime and dev requirements files"
```

---

## Task 2: Write CLAUDE.md

**Files:**
- Create: `CLAUDE.md`

- [ ] **Step 1: Write CLAUDE.md**

```markdown
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

```
IDLE ──► LAUNCHING ──► PULLING_IMAGE ──► LOADING ──► READY
  ▲                                         │           │
  └──────────── STOPPING ◄──────────────────┘           │
  ▲                                                      │
  └──────────── ERROR ◄─────────────────────────────────┘
```

## Adding a new feature to both UIs

1. Add `on_<feature>` callback to `AppController` (in `controller.py`)
2. Add `on_<feature>` to `ViewContract` in `tests/test_controller_contract.py`
3. Run tests — contract tests fail on both stubs
4. Implement `on_<feature>` in `GtkViewStub`, `TuiViewStub`, `MainWindow`, and TUI widget
5. Tests pass

## File layout

```
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
```

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
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add CLAUDE.md with architecture and threading guide"
```

---

## Task 3: Write failing contract tests

**Files:**
- Create: `tests/test_controller_contract.py`

These tests fail until Task 4 creates `AppController`.

- [ ] **Step 1: Write the test file**

```python
# tests/test_controller_contract.py
"""ViewContract — mechanically enforces GUI/TUI feature parity.

If AppController gains a new on_* callback, this test fails until both
GtkViewStub and TuiViewStub implement it.  Add the new method to both
stubs AND to the ABC to restore green.
"""
from abc import ABC, abstractmethod
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


class ViewContract(ABC):
    """Every on_* callback that AppController can emit MUST be handled here."""

    @abstractmethod
    def on_state_changed(self, state, info: str): ...

    @abstractmethod
    def on_log_line(self, line: str): ...

    @abstractmethod
    def on_progress(self, fraction: float, label: str): ...

    @abstractmethod
    def on_substage(self, stepper: str, tour_left: str, tour_right: str, dots: str): ...

    @abstractmethod
    def on_catalog_loaded(self, catalog, compatible_devices: list): ...

    @abstractmethod
    def on_cache_scanned(self, info): ...

    @abstractmethod
    def on_bench_progress(self, line: str): ...

    @abstractmethod
    def on_bench_result(self, result): ...

    @abstractmethod
    def on_tool_result(self, result): ...


class GtkViewStub(ViewContract):
    def on_state_changed(self, state, info): pass
    def on_log_line(self, line): pass
    def on_progress(self, fraction, label): pass
    def on_substage(self, stepper, tour_left, tour_right, dots): pass
    def on_catalog_loaded(self, catalog, compatible_devices): pass
    def on_cache_scanned(self, info): pass
    def on_bench_progress(self, line): pass
    def on_bench_result(self, result): pass
    def on_tool_result(self, result): pass


class TuiViewStub(ViewContract):
    def on_state_changed(self, state, info): pass
    def on_log_line(self, line): pass
    def on_progress(self, fraction, label): pass
    def on_substage(self, stepper, tour_left, tour_right, dots): pass
    def on_catalog_loaded(self, catalog, compatible_devices): pass
    def on_cache_scanned(self, info): pass
    def on_bench_progress(self, line): pass
    def on_bench_result(self, result): pass
    def on_tool_result(self, result): pass


def test_gtk_stub_satisfies_contract():
    assert isinstance(GtkViewStub(), ViewContract)


def test_tui_stub_satisfies_contract():
    assert isinstance(TuiViewStub(), ViewContract)


def test_controller_on_attrs_match_contract():
    """AppController's on_* attributes must exactly match ViewContract methods."""
    from controller import AppController
    controller_cbs = {
        a for a in vars(AppController).get("__annotations__", {})
        if a.startswith("on_")
    }
    # Also pick up any on_* set in __init__ that aren't annotated at class level
    ctrl = AppController()
    instance_cbs = {a for a in vars(ctrl) if a.startswith("on_")}
    all_ctrl_cbs = controller_cbs | instance_cbs

    contract_methods = {
        a for a in dir(ViewContract)
        if a.startswith("on_") and callable(getattr(ViewContract, a))
    }
    assert all_ctrl_cbs == contract_methods, (
        f"Controller has callbacks not in contract: {all_ctrl_cbs - contract_methods}\n"
        f"Contract has methods not in controller: {contract_methods - all_ctrl_cbs}"
    )
```

- [ ] **Step 2: Run to confirm it fails**

```bash
PYTHONPATH=app pytest tests/test_controller_contract.py -v
```

Expected: `ImportError: No module named 'controller'` (AppController doesn't exist yet).

- [ ] **Step 3: Commit the failing tests**

```bash
git add tests/test_controller_contract.py
git commit -m "test: add ViewContract ABC — fails until AppController exists"
```

---

## Task 4: Create AppController skeleton

**Files:**
- Create: `app/controller.py`

Skeleton with all `on_*` callbacks defined; no logic yet. Makes contract tests pass.

- [ ] **Step 1: Create app/controller.py**

```python
# app/controller.py
# SPDX-License-Identifier: Apache-2.0
"""AppController — owns the server lifecycle state machine.

Both GTK GUI and Textual TUI are thin views that register on_* callbacks.
No GTK or Textual imports anywhere in this file.

Threading discipline:
    All on_* callbacks are dispatched through self._dispatch(fn, *args).
    Background threading.Timer callbacks call self._emit(), which posts
    to the UI event loop — never touching widgets directly.
"""
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from app_settings import settings as _settings
from device_detector import detect_devices
from health_worker import HealthWorker
from hf_cache import ModelCacheInfo, scan_model_cache
from launch_options import LaunchOptions
from model_catalog import ModelCatalog, ModelEntry
from server_manager import LaunchConfig, ServerManager, ServerState
from timing_store import TimingStore

_CONFIG_DIR = Path.home() / ".config" / "tt-runner-gui"
_TIMING_PATH = _CONFIG_DIR / "timing.json"

# ── Stage/tour constants (shared with views for display labels) ──────────────
STAGE_LABELS: dict = {
    "engine_init":     "Engine Init",
    "device_setup":    "Device Mesh",
    "loading_weights": "Weights",
    "kv_cache":        "KV Cache",
    "api_startup":     "API Server",
    "trace_capture":   "Trace Capture",
    "device_init":     "Device Init",
    "mesh_created":    "Mesh",
    "cache_loading":   "TT Cache",
    "model_loaded":    "Model",
    "warmup":          "Warmup",
    "warmup_complete": "Warmup",
}

VLLM_STAGES  = ["engine_init", "device_setup", "loading_weights", "kv_cache", "api_startup", "trace_capture"]
MEDIA_STAGES = ["device_init", "mesh_created", "loading_weights", "cache_loading", "model_loaded", "warmup"]

# Tour card text — keyed by substage name, list of rotating cards
TOUR_CARDS: dict = {
    "engine_init": [
        "TT Metal opens PCIe connections to each Tenstorrent chip and verifies firmware. No model weights loaded yet — this is pure hardware bring-up.",
        "Each Tenstorrent chip has 108 Tensix cores in a 12×9 grid. Each core has 1.5 MB of local SRAM and a dedicated matrix/vector compute unit that runs independently.",
        "An embedded RISC-V management CPU on each chip handles DMA scheduling, Ethernet link setup, and power management while Tensix cores later run inference.",
    ],
    "device_setup": [
        "Tensor parallelism: weight matrices are sharded column-wise across chips via Ethernet fabric. Each chip holds 1/N of every weight tensor.",
        "The Ethernet links between chips run at 100 Gb/s. An allreduce across 4 chips takes ~1 µs — far less than a single transformer layer's compute time.",
        "Column-parallel sharding means each chip computes attention for a different subset of heads. With GQA the KV heads are fewer, reducing KV replication cost.",
    ],
    "loading_weights": [
        "Weight shards stream from disk into each chip's DRAM via PCIe (~7 GB/s per chip). The bottleneck is disk→DRAM transfer, not computation.",
        "Weights arrive as bfloat16 (2 bytes/element). The chips support on-the-fly quantization to int8 to halve DRAM bandwidth during actual inference.",
        "Attention weight matrices (Q, K, V, O_proj) are column-sharded across chips. Each chip's Q_proj is [hidden × (hidden÷N)] where N is the chip count.",
    ],
    "kv_cache": [
        "KV cache is pre-allocated in chip SRAM before the first token. Accessing SRAM takes <1 µs vs. ~100 ns for DRAM — critical for fast autoregressive decode.",
        "Each layer needs 2 × context_length × head_dim × num_kv_heads elements for K and V. With GQA the KV tensor is much smaller than the full attention map.",
        "Paged KV attention divides the cache into fixed-size blocks (e.g., 16 tokens each), avoiding fragmentation and enabling efficient batch scheduling.",
    ],
    "api_startup": [
        "The HTTP server starts accepting connections. The model is fully on-device but execution graphs haven't been JIT-compiled for every context length yet.",
        "vLLM uses continuous batching: new requests join the decode batch mid-generation, keeping hardware utilization high even with uneven arrival rates.",
        "The vLLM scheduler can preempt a partially-decoded sequence and swap it out when memory is needed — enabling fair multi-tenant serving without starvation.",
    ],
    "trace_capture": [
        "vLLM JIT-compiles a separate execution graph for each of 10 context lengths (128→65408 tokens). After capture, every inference replays a pre-built trace.",
        "Trace capture = graph compilation: TT Metal unrolls every op into a static kernel-dispatch sequence. At inference time there is zero Python GIL overhead.",
        "Each of the 10 traces is specialized for its sequence length — the compiler tiles and schedules operations differently for a 128-token vs. 65408-token batch.",
    ],
    "device_init": [
        "The media server initializes the device mesh and allocates shared memory pools across all dies. This is the first time the hardware is exercised.",
        "On a Galaxy (8× P150), the mesh is 8 chips in a ring topology. Each chip sees a unified virtual address space backed by its own 12 GB LPDDR5 DRAM.",
        "Device init checks firmware versions and calibrates thermal sensors. If any chip is above the thermal threshold, the server refuses to start.",
    ],
    "mesh_created": [
        "A 2D chip mesh is established. Activations flow over the on-package Ethernet fabric rather than through host DRAM — reducing round-trip latency by ~100×.",
        "The mesh topology determines model partitioning. For video: spatial encoder on one chip set, temporal decoder on another, pipelined across the fabric.",
        "Collective operations (AllReduce, AllGather) use ring algorithms that saturate the Ethernet links without touching host memory.",
    ],
    "cache_loading": [
        "Pre-compiled TT Metal kernel binaries are loaded from disk cache. A cache hit avoids LLVM compilation — saving minutes per model load on first boot.",
        "The tensor cache keys on op type + tensor shape + data type + chip generation. A Wormhole kernel binary won't be used on Blackhole — different ISA.",
        "The cache is invalidated on firmware update to prevent ABI mismatches. After an upgrade the first load is slower; subsequent loads are fast again.",
    ],
    "model_loaded": [
        "All model components — transformer, text encoder, and VAE decoder — are resident on-chip and ready for the warmup pass.",
        "For diffusion models the pipeline is: text encoder (CLIP or T5) → denoising U-Net or DiT → VAE decoder. All three sub-models are pre-loaded onto chips.",
        "The VAE decoder is the final inference step, converting a latent 64×64 tensor into a 1024×1024 pixel image. It's the compute-heaviest part per output pixel.",
    ],
    "warmup": [
        "Warmup runs 2 full denoising passes to JIT-compile TT Metal kernels and capture execution traces. One-time cost per boot; subsequent inferences are fast.",
        "WAN 2.2 uses ~50 denoising timesteps per video. Each warmup pass compiles attention and FFN kernels for that specific resolution and batch size.",
        "After warmup the compiled kernels are stored in SRAM. Subsequent inferences skip compilation and replay the recorded kernel dispatch sequence directly.",
    ],
    "warmup_complete": [
        "Warmup complete! The server is now fully primed: kernels compiled, traces captured, KV cache allocated. Waiting for health check.",
        "The health endpoint (/tt-liveness or /v1/models) returns 200 once the server thread is ready — this guard prevents routing traffic before the model is live.",
        "First inference will be nearly as fast as steady-state. Compile-time overhead was paid during warmup; the hot path is now pure kernel replay.",
    ],
}


@dataclass
class ToolRoundTrip:
    step: str        # "call" | "result" | "final"
    name: str        # tool name (for "call" step)
    arguments: str   # JSON string (for "call" step)
    content: str     # result payload or final assistant text


@dataclass
class BenchResult:
    model_name: str
    device: str
    timestamp: str
    isl: int
    osl: int
    concurrency: int
    mean_ttft_ms: float
    p95_ttft_ms: Optional[float]
    mean_tps: float
    tps_decode: float
    mean_e2el_ms: float
    request_throughput: float
    tier_pass: str   # "PASS" | "BELOW_TARGET" | "FAIL"


class AppController:
    """Owns the inference server lifecycle.  Views register on_* callbacks."""

    def __init__(self, dispatch_fn: Optional[Callable] = None):
        # dispatch_fn(fn, *args) posts fn(*args) to the UI event loop.
        # Defaults to synchronous direct call (safe for tests).
        self._dispatch: Callable = dispatch_fn or (lambda fn, *a: fn(*a))

        self._server_mgr = ServerManager()
        self._health_worker: Optional[HealthWorker] = None
        self._timing = TimingStore(_TIMING_PATH)
        self._catalog: Optional[ModelCatalog] = None
        self._current_entry: Optional[ModelEntry] = None
        self._cache_info: Optional[ModelCacheInfo] = None
        self._state = ServerState.IDLE
        self._load_start: Optional[float] = None
        self._progress_timer: Optional[threading.Timer] = None
        self._tour_timer: Optional[threading.Timer] = None
        self._tour_card_idx: int = 0
        self._tour_substage: Optional[str] = None
        self._options = LaunchOptions()

        # Callbacks — views set these after construction; None = ignored
        self.on_state_changed: Optional[Callable] = None   # (ServerState, str)
        self.on_log_line: Optional[Callable] = None         # (str,)
        self.on_progress: Optional[Callable] = None         # (float, str)
        self.on_substage: Optional[Callable] = None         # (str, str, str, str)
        self.on_catalog_loaded: Optional[Callable] = None   # (ModelCatalog, List[str])
        self.on_cache_scanned: Optional[Callable] = None    # (ModelCacheInfo,)
        self.on_bench_progress: Optional[Callable] = None   # (str,)
        self.on_bench_result: Optional[Callable] = None     # (BenchResult,)
        self.on_tool_result: Optional[Callable] = None      # (ToolRoundTrip,)

    # ── Read-only properties for views ──────────────────────────────────────

    @property
    def state(self) -> ServerState:
        return self._state

    @property
    def current_entry(self) -> Optional[ModelEntry]:
        return self._current_entry

    @property
    def catalog(self) -> Optional[ModelCatalog]:
        return self._catalog

    # ── Internal helpers ────────────────────────────────────────────────────

    def _emit(self, cb_name: str, *args) -> None:
        """Dispatch a named callback through dispatch_fn if it is registered."""
        cb = getattr(self, cb_name, None)
        if cb is not None:
            self._dispatch(cb, *args)

    # ── Public interface — called by views ───────────────────────────────────

    def load_repo(self, path: Path) -> None:
        """Parse model_spec.json and detect devices in a background thread."""
        pass  # implemented in Task 6

    def select_model(self, entry: ModelEntry) -> None:
        """Called when user selects a model; triggers HF cache scan."""
        pass  # implemented in Task 6

    def launch(self, entry: ModelEntry, port: str,
               options: Optional[LaunchOptions] = None) -> None:
        """Start the inference server for the given model entry."""
        pass  # implemented in Task 6

    def stop(self) -> None:
        """Stop the running server."""
        pass  # implemented in Task 6

    def get_options(self) -> LaunchOptions:
        return self._options

    def set_options(self, options: LaunchOptions) -> None:
        self._options = options

    def run_benchmark(self, mode: str = "smoke-test",
                      concurrency_sweeps: bool = False,
                      percentile_report: bool = False) -> None:
        """Wrap run.py --workflow benchmarks (implemented in Plan 2)."""
        pass

    def send_tool_call(self, tools: list, prompt: str) -> None:
        """Send a tool-call round-trip to the running server (Plan 2)."""
        pass
```

- [ ] **Step 2: Run contract tests — should now pass**

```bash
PYTHONPATH=app pytest tests/test_controller_contract.py -v
```

Expected output:
```
test_controller_contract.py::test_gtk_stub_satisfies_contract PASSED
test_controller_contract.py::test_tui_stub_satisfies_contract PASSED
test_controller_contract.py::test_controller_on_attrs_match_contract PASSED
```

- [ ] **Step 3: Commit**

```bash
git add app/controller.py
git commit -m "feat: add AppController skeleton — contract tests pass"
```

---

## Task 5: Write failing controller unit tests

**Files:**
- Create: `tests/test_controller.py`

- [ ] **Step 1: Write test_controller.py**

```python
# tests/test_controller.py
"""AppController unit tests — no GTK, no Textual.

Uses NullDispatch (synchronous direct call) so callbacks fire inline and
assertions run immediately after the triggering method.
"""
import sys, os, time, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest
from unittest.mock import MagicMock, patch, call
from controller import AppController
from server_manager import ServerState


# ── Test double ──────────────────────────────────────────────────────────────

class RecordingView:
    """Captures all callback invocations for assertions."""
    def __init__(self):
        self.states: list = []       # [(ServerState, str), ...]
        self.log_lines: list = []    # [str, ...]
        self.progress: list = []     # [(float, str), ...]
        self.substages: list = []    # [(stepper, left, right, dots), ...]

    def on_state_changed(self, state, info): self.states.append((state, info))
    def on_log_line(self, line): self.log_lines.append(line)
    def on_progress(self, fraction, label): self.progress.append((fraction, label))
    def on_substage(self, stepper, left, right, dots): self.substages.append((stepper, left, right, dots))
    def on_catalog_loaded(self, catalog, devices): pass
    def on_cache_scanned(self, info): pass
    def on_bench_progress(self, line): pass
    def on_bench_result(self, result): pass
    def on_tool_result(self, result): pass


def make_controller() -> tuple:
    """Return (AppController, RecordingView) wired with NullDispatch."""
    ctrl = AppController(dispatch_fn=lambda fn, *a: fn(*a))
    view = RecordingView()
    ctrl.on_state_changed  = view.on_state_changed
    ctrl.on_log_line       = view.on_log_line
    ctrl.on_progress       = view.on_progress
    ctrl.on_substage       = view.on_substage
    ctrl.on_catalog_loaded = view.on_catalog_loaded
    ctrl.on_cache_scanned  = view.on_cache_scanned
    ctrl.on_bench_progress = view.on_bench_progress
    ctrl.on_bench_result   = view.on_bench_result
    ctrl.on_tool_result    = view.on_tool_result
    return ctrl, view


# ── State machine ────────────────────────────────────────────────────────────

def test_initial_state_is_idle():
    ctrl, _ = make_controller()
    assert ctrl.state == ServerState.IDLE


def test_vllm_engine_init_log_triggers_loading():
    ctrl, view = make_controller()
    ctrl._handle_log_line("Automatically detected platform tt")
    assert any(s == ServerState.LOADING for s, _ in view.states)


def test_media_server_init_log_triggers_loading():
    ctrl, view = make_controller()
    ctrl._handle_log_line("Creating new Video service")
    assert any(s == ServerState.LOADING for s, _ in view.states)


def test_docker_pull_log_triggers_pulling_image():
    ctrl, view = make_controller()
    ctrl._handle_log_line("docker pull ghcr.io/tenstorrent/tt-inference-server:latest")
    assert any(s == ServerState.PULLING_IMAGE for s, _ in view.states)


def test_no_duplicate_state_transition():
    ctrl, view = make_controller()
    ctrl._handle_log_line("Automatically detected platform tt")
    count_before = len(view.states)
    ctrl._handle_log_line("Automatically detected platform tt")
    # Second identical trigger should not emit a second transition
    assert len(view.states) == count_before


# ── Progress ticks ───────────────────────────────────────────────────────────

def _make_loading_controller():
    ctrl, view = make_controller()
    ctrl._state = ServerState.LOADING
    ctrl._load_start = time.monotonic() - 10.0
    ctrl._current_entry = MagicMock(
        hf_model_repo="meta-llama/Llama-3.1-8B-Instruct",
        device_type="N150",
        inference_engine="vllm",
        min_disk_gb=15.0,
        family="Llama",
    )
    return ctrl, view


def test_trace_capture_progress_fraction():
    ctrl, view = _make_loading_controller()
    # Simulate 5 of 10 trace captures logged
    for seq_len in [128, 256, 512, 1024, 2048]:
        ctrl._handle_log_line(f"Capturing traces: input_seq_len={seq_len}")
    ctrl._progress_tick()
    fractions = [f for f, _ in view.progress]
    assert fractions, "No progress emitted"
    assert abs(fractions[-1] - 0.5) < 0.05, f"Expected ~0.5, got {fractions[-1]}"


def test_warmup_progress_fraction():
    ctrl, view = _make_loading_controller()
    ctrl._server_mgr.parser.warmup_n = 1
    ctrl._server_mgr.parser.warmup_total = 2
    ctrl._progress_tick()
    fractions = [f for f, _ in view.progress]
    assert fractions
    assert abs(fractions[-1] - 0.5) < 0.05


# ── Substage / stepper ───────────────────────────────────────────────────────

def test_substage_emitted_on_log_line():
    ctrl, view = _make_loading_controller()
    ctrl._state = ServerState.LOADING
    ctrl._handle_log_line("Automatically detected platform tt")
    ctrl._handle_log_line("Loading checkpoint shards")
    assert any("Weights" in stepper for stepper, *_ in view.substages), (
        f"Expected 'Weights' in stepper, got: {view.substages}"
    )


def test_stepper_marks_completed_stages():
    ctrl, view = _make_loading_controller()
    ctrl._state = ServerState.LOADING
    ctrl._server_mgr.parser.last_substage = "loading_weights"
    text = ctrl._build_stepper_text("loading_weights")
    assert "✓" in text, "Completed stages should have ✓"
    assert "●" in text, "Active stage should have ●"
    assert "○" in text, "Future stages should have ○"


# ── _emit with dispatch ───────────────────────────────────────────────────────

def test_emit_uses_dispatch_fn():
    dispatched = []
    ctrl = AppController(dispatch_fn=lambda fn, *a: dispatched.append((fn, a)))
    recorded = []
    ctrl.on_log_line = lambda line: recorded.append(line)
    ctrl._emit("on_log_line", "hello")
    # dispatch was called but recorded is still empty (dispatch captured, not called)
    assert len(dispatched) == 1
    assert dispatched[0][1] == ("hello",)


def test_null_dispatch_calls_directly():
    ctrl, view = make_controller()
    ctrl._emit("on_log_line", "test line")
    assert "test line" in view.log_lines


def test_missing_callback_does_not_raise():
    ctrl = AppController()
    # No callbacks registered — _emit should silently do nothing
    ctrl._emit("on_log_line", "ignored")
```

- [ ] **Step 2: Run to confirm failures**

```bash
PYTHONPATH=app pytest tests/test_controller.py -v 2>&1 | head -40
```

Expected: `AttributeError: 'AppController' object has no attribute '_handle_log_line'` (method not yet implemented).

- [ ] **Step 3: Commit**

```bash
git add tests/test_controller.py
git commit -m "test: add AppController unit tests — failing until Task 6"
```

---

## Task 6: Implement AppController body

**Files:**
- Modify: `app/controller.py` — fill in all `pass` stubs

The business logic moves verbatim from `main_window.py` with these mechanical changes:
- Replace `GLib.timeout_add(ms, fn)` with `threading.Timer(ms/1000, fn_loop)` (repeating pattern)
- Replace `idle_add_once(cb, *args)` with `self._emit("on_*", *args)`
- Replace `self._panel.set_state(...)` etc. with `self._emit("on_state_changed", ...)`

- [ ] **Step 1: Implement load_repo and select_model in app/controller.py**

Add these methods, replacing the `pass` stubs:

```python
def load_repo(self, path: Path) -> None:
    spec = path / "model_spec.json"
    if not spec.exists():
        self._emit("on_log_line", f"⚠ model_spec.json not found at {path}")
        return
    try:
        self._catalog = ModelCatalog.load(spec)
    except Exception as e:
        self._emit("on_log_line", f"⚠ Failed to parse model_spec.json: {e}")
        return

    _settings.server_repo_path = str(path)
    _settings.save()
    self._emit("on_log_line",
               f"Loaded {len(self._catalog.all_entries())} model configurations from {spec}")

    def _detect():
        devices = detect_devices()
        compatible = devices if devices else self._catalog.all_device_types()
        if not devices:
            self._emit("on_log_line", "⚠ tt-smi not found — showing all devices")
        self._emit("on_catalog_loaded", self._catalog, compatible)

    threading.Thread(target=_detect, daemon=True).start()


def select_model(self, entry: ModelEntry) -> None:
    self._current_entry = entry
    self._cache_info = None

    def _scan():
        info = scan_model_cache(entry.hf_model_repo)
        self._cache_info = info
        self._emit("on_cache_scanned", info)

    threading.Thread(target=_scan, daemon=True).start()
```

- [ ] **Step 2: Implement _read_hf_token and launch in app/controller.py**

```python
def _read_hf_token(self, repo_path: Path) -> Optional[str]:
    token = os.environ.get("HF_TOKEN", "")
    if token:
        return token
    env_file = repo_path / ".env"
    if env_file.exists():
        for line in env_file.read_text(errors="replace").splitlines():
            if line.startswith("HF_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                if token:
                    return token
    return None


def launch(self, entry: ModelEntry, port: str,
           options: Optional[LaunchOptions] = None) -> None:
    if self._state not in (ServerState.IDLE, ServerState.ERROR):
        return
    self._current_entry = entry
    if options:
        self._options = options

    repo_path = Path(_settings.server_repo_path)
    if not (repo_path / "run.py").exists():
        self._emit("on_log_line", f"⚠ run.py not found at {repo_path}")
        return

    hf_token = self._read_hf_token(repo_path)
    if not hf_token:
        self._emit("on_log_line",
                   "⚠ HF_TOKEN not found in environment or .env — launch may fail")

    config = LaunchConfig(
        repo_path=repo_path,
        model_name=entry.display_name,
        device=entry.device_type,
        port=port,
        hf_token=hf_token,
        no_auth=True,
        options=self._options,
        inference_engine=entry.inference_engine,
    )

    self._emit("on_log_line",
               f"▶ Launching {entry.display_name} on {entry.device_type} · port {port}")
    self._transition(ServerState.LAUNCHING)

    self._health_worker = HealthWorker(
        port=port,
        on_ready=self._on_health_ready,
        on_lost=self._on_health_lost,
        engine="media" if entry.inference_engine == "media" else "vllm",
        dispatch_fn=self._dispatch,
    )
    self._health_worker.start()
    self._server_mgr.launch(config, self._handle_log_line, self._on_server_state)


def stop(self) -> None:
    self._transition(ServerState.STOPPING)
    if self._health_worker:
        self._health_worker.stop()
        self._health_worker = None
    self._server_mgr.stop()
    # Force idle after 10s if docker stop is slow
    t = threading.Timer(10.0, self._force_idle)
    t.daemon = True
    t.start()


def _force_idle(self) -> None:
    if self._state == ServerState.STOPPING:
        self._transition(ServerState.IDLE)
```

- [ ] **Step 3: Implement _handle_log_line and _on_server_state in app/controller.py**

```python
def _handle_log_line(self, line: str) -> None:
    self._emit("on_log_line", line)
    new_state = self._server_mgr.parser.feed(line)
    if new_state:
        self._transition(new_state)

    if self._state == ServerState.LOADING:
        substage = self._server_mgr.parser.last_substage
        if substage:
            stepper = self._build_stepper_text(substage)
            if substage != self._tour_substage:
                self._tour_substage = substage
                self._tour_card_idx = 0
            left, right = self._build_tour_content(substage)
            cards = TOUR_CARDS.get(substage, [])
            total = len(cards)
            dots = ("  ".join("●" if i == self._tour_card_idx else "○"
                              for i in range(total)) if total > 1 else "")
            self._emit("on_substage", stepper, left, right, dots)


def _on_server_state(self, state: ServerState) -> None:
    self._transition(state)


def _on_health_ready(self, models: list) -> None:
    if self._state in (ServerState.LOADING, ServerState.LAUNCHING,
                       ServerState.PULLING_IMAGE):
        self._transition(ServerState.READY)
        mstr = ", ".join(models) if models else "ready"
        port = _settings.last_port
        self._emit("on_state_changed", ServerState.READY,
                   f"localhost:{port}  ·  {mstr}")
        if self._load_start and self._current_entry:
            dur = time.monotonic() - self._load_start
            self._timing.record_load(
                self._current_entry.hf_model_repo,
                self._current_entry.device_type,
                dur, cold=False,
            )


def _on_health_lost(self) -> None:
    if self._state == ServerState.READY:
        self._transition(ServerState.ERROR)
        self._emit("on_log_line",
                   "⚠ Health check lost — server may have crashed")
```

- [ ] **Step 4: Implement _transition and timer management in app/controller.py**

```python
def _transition(self, state: ServerState) -> None:
    if state == self._state:
        return
    self._state = state

    info = ""
    if self._current_entry:
        port = _settings.last_port
        info = (f"localhost:{port}  ·  {self._current_entry.display_name}"
                f"  ·  {self._current_entry.device_type}")

    self._emit("on_state_changed", state, info)

    if state == ServerState.LOADING:
        self._load_start = time.monotonic()
        self._tour_card_idx = 0
        self._tour_substage = None
        self._start_progress_ticker()
        self._start_tour_timer()
    elif state in (ServerState.READY, ServerState.ERROR,
                   ServerState.IDLE, ServerState.STOPPING):
        self._stop_progress_ticker()
        self._stop_tour_timer()


def _start_progress_ticker(self) -> None:
    self._stop_progress_ticker()

    def _loop():
        if self._state != ServerState.LOADING:
            return
        self._progress_tick()
        self._progress_timer = threading.Timer(1.0, _loop)
        self._progress_timer.daemon = True
        self._progress_timer.start()

    self._progress_timer = threading.Timer(1.0, _loop)
    self._progress_timer.daemon = True
    self._progress_timer.start()


def _stop_progress_ticker(self) -> None:
    if self._progress_timer:
        self._progress_timer.cancel()
        self._progress_timer = None


def _start_tour_timer(self) -> None:
    self._stop_tour_timer()

    def _loop():
        if self._state != ServerState.LOADING:
            return
        substage = self._tour_substage or ""
        cards = TOUR_CARDS.get(substage, [])
        if len(cards) > 1:
            self._tour_card_idx = (self._tour_card_idx + 1) % len(cards)
            left, right = self._build_tour_content(substage)
            dots = "  ".join(
                "●" if i == self._tour_card_idx else "○"
                for i in range(len(cards))
            )
            self._emit("on_substage",
                       self._build_stepper_text(substage), left, right, dots)
        self._tour_timer = threading.Timer(12.0, _loop)
        self._tour_timer.daemon = True
        self._tour_timer.start()

    self._tour_timer = threading.Timer(12.0, _loop)
    self._tour_timer.daemon = True
    self._tour_timer.start()


def _stop_tour_timer(self) -> None:
    if self._tour_timer:
        self._tour_timer.cancel()
        self._tour_timer = None
```

- [ ] **Step 5: Implement _progress_tick in app/controller.py**

```python
def _progress_tick(self) -> None:
    if self._state != ServerState.LOADING or not self._current_entry:
        return
    elapsed = time.monotonic() - (self._load_start or time.monotonic())
    parser = self._server_mgr.parser

    if parser.trace_capture_count > 0:
        frac = parser.trace_capture_count / 10.0
        remaining = (10 - parser.trace_capture_count) * 3
        self._emit("on_progress", frac,
                   f"Capturing traces {parser.trace_capture_count}/10"
                   f" · ~{remaining:.0f}s remaining")
        return

    if parser.warmup_n is not None and parser.warmup_total:
        frac = parser.warmup_n / parser.warmup_total
        est = self._timing.estimate_substage(
            self._current_entry.hf_model_repo,
            self._current_entry.device_type, "warmup",
        )
        label = f"Warmup {parser.warmup_n}/{parser.warmup_total}"
        if est.seconds:
            per_step = est.seconds / parser.warmup_total
            remaining = per_step * (parser.warmup_total - parser.warmup_n)
            label += f" · ~{remaining:.0f}s remaining · {est.source}"
        self._emit("on_progress", frac, label)
        return

    est = self._timing.estimate_load(
        self._current_entry.hf_model_repo,
        self._current_entry.device_type,
        cold=False,
        size_gb=self._current_entry.min_disk_gb or 10.0,
        family=self._current_entry.family,
    )
    if est.seconds and est.seconds > 0:
        frac = min(elapsed / est.seconds, 0.95)
        remaining = max(est.seconds - elapsed, 0)
        m, s = divmod(int(remaining), 60)
        ts = f"{m}m {s}s" if m else f"{s}s"
        self._emit("on_progress", frac, f"~{ts} remaining · {est.source}")
    else:
        self._emit("on_progress", -1.0, "")  # -1 signals pulse mode to view
```

- [ ] **Step 6: Implement _build_stepper_text and _build_tour_content in app/controller.py**

```python
def _build_stepper_text(self, substage: str) -> str:
    if not self._current_entry:
        return ""
    stages = (MEDIA_STAGES if self._current_entry.inference_engine == "media"
              else VLLM_STAGES)
    parts = []
    found_active = False
    for s in stages:
        lbl = STAGE_LABELS.get(s, s)
        if s == substage:
            parts.append(f"● {lbl}")
            found_active = True
        elif not found_active:
            parts.append(f"✓ {lbl}")
        else:
            parts.append(f"○ {lbl}")
    return "  ──  ".join(parts)


def _build_tour_left(self, entry: ModelEntry) -> str:
    ci = self._cache_info
    lines = [f"📁 {entry.hf_model_repo}"]
    if ci and ci.is_cached:
        lines.append("  ✓ cached locally")
        if ci.safetensors:
            gb = ci.total_bytes / 1e9
            lines.append(f"  {len(ci.safetensors)} shards · {gb:.1f} GB")
        else:
            other_gb = ci.total_bytes / 1e9
            if other_gb > 0:
                lines.append(f"  {other_gb:.1f} GB total")
        a = ci.arch
        if a and a.num_layers:
            lines.append(f"  {a.num_layers} layers · hidden={a.hidden_size}")
            if a.num_kv_heads and a.num_kv_heads != a.num_heads:
                lines.append(f"  GQA: {a.num_heads}Q / {a.num_kv_heads}KV heads")
            elif a.num_heads:
                lines.append(f"  {a.num_heads} heads · head_dim={a.head_dim}")
            if a.context_length:
                lines.append(f"  ctx={a.context_length:,} tokens")
    elif ci and not ci.is_cached:
        lines.append("  ○ not in local HF cache")
        if entry.min_disk_gb:
            lines.append(f"  ~{entry.min_disk_gb:.0f} GB on disk")
    else:
        if entry.min_disk_gb:
            lines.append(f"  ~{entry.min_disk_gb:.0f} GB on disk")
        if entry.param_count:
            lines.append(f"  {entry.param_count:.0f}B parameters")
    lines.append(f"  Engine: {entry.inference_engine}")
    lines.append(f"  Status: {entry.status}")
    return "\n".join(lines)


def _build_tour_content(self, substage: Optional[str]) -> tuple:
    entry = self._current_entry
    if not entry:
        return ("", "")
    left = self._build_tour_left(entry)
    cards = TOUR_CARDS.get(substage or "", [])
    right = (cards[self._tour_card_idx % len(cards)]
             if cards else "Loading model onto Tenstorrent hardware…")
    return (left, right)
```

- [ ] **Step 7: Run controller tests — all should pass**

```bash
PYTHONPATH=app pytest tests/test_controller.py tests/test_controller_contract.py -v
```

Expected: all tests pass. If any fail, fix before continuing.

- [ ] **Step 8: Commit**

```bash
git add app/controller.py
git commit -m "feat: implement AppController — state machine, timers, progress, tour"
```

---

## Task 7: Refactor worker.py — remove GLib

**Files:**
- Modify: `app/worker.py`

`health_worker.py` and `server_manager.py` still import `idle_add_once`. We convert `worker.py` to a pure shim with a module-level setter. The GTK layer sets it; non-GTK code gets synchronous passthrough.

- [ ] **Step 1: Replace app/worker.py entirely**

```python
# app/worker.py
# SPDX-License-Identifier: Apache-2.0
"""Dispatch shim — backward-compatible wrapper used by health_worker and
server_manager while they are being migrated to AppController.

GTK main.py calls set_dispatch(GLib.idle_add) at startup so that legacy
callers still post to the GTK main thread.  The TUI sets its own dispatch.
New code should use AppController._emit() instead of this module.
"""
from typing import Any, Callable

_dispatch: Callable = lambda fn, *a: fn(*a)


def set_dispatch(fn: Callable) -> None:
    """Set the event-loop dispatch function for this process.

    Call once at startup before any background threads start.
    fn(callback, *args) must schedule callback(*args) on the UI event loop.
    """
    global _dispatch
    _dispatch = fn


def idle_add_once(fn: Callable, *args: Any) -> None:
    """Schedule fn(*args) on the UI event loop via the registered dispatch fn."""
    _dispatch(fn, *args)
```

- [ ] **Step 2: Verify existing tests still pass**

```bash
PYTHONPATH=app pytest tests/ -v --ignore=tests/test_controller.py \
  --ignore=tests/test_controller_contract.py
```

Expected: all pre-existing tests pass (they don't exercise the dispatch path directly).

- [ ] **Step 3: Commit**

```bash
git add app/worker.py
git commit -m "refactor: worker.py — remove GLib import, add set_dispatch() shim"
```

---

## Task 8: Refactor health_worker.py — accept dispatch_fn

**Files:**
- Modify: `app/health_worker.py`

`HealthWorker` currently calls `idle_add_once` from worker.  AppController
passes its own `dispatch_fn` so the worker posts to the right event loop.

- [ ] **Step 1: Replace health_worker.py**

```python
# app/health_worker.py
# SPDX-License-Identifier: Apache-2.0
"""Background thread polling /v1/models (vLLM) or /tt-liveness (media server).

Callbacks are posted through dispatch_fn — never touches widgets directly.
AppController injects its dispatch_fn at construction time.
"""
import threading
from typing import Callable, List, Optional

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


class HealthWorker(threading.Thread):
    def __init__(
        self,
        port: str,
        on_ready: Callable[[List[str]], None],
        on_lost: Callable[[], None],
        dispatch_fn: Optional[Callable] = None,
        poll_interval: float = 5.0,
        engine: str = "vllm",
    ):
        super().__init__(daemon=True, name="HealthWorker")
        self._port = port
        self._on_ready = on_ready
        self._on_lost = on_lost
        self._dispatch = dispatch_fn or (lambda fn, *a: fn(*a))
        self._poll_interval = poll_interval
        self._engine = engine
        self._stop = threading.Event()
        self._was_ready = False

    def stop(self):
        self._stop.set()

    def _check(self) -> Optional[List[str]]:
        if not _HAS_REQUESTS:
            return None
        try:
            if self._engine == "media":
                r = _requests.get(
                    f"http://localhost:{self._port}/tt-liveness", timeout=3)
                if r.status_code == 200:
                    return [r.json().get("model", "unknown")]
            else:
                r = _requests.get(
                    f"http://localhost:{self._port}/v1/models", timeout=3)
                if r.status_code == 200:
                    data = r.json()
                    return [m.get("id", "unknown") for m in data.get("data", [])]
        except Exception:
            pass
        return None

    def run(self):
        while not self._stop.is_set():
            models = self._check()
            if models is not None and not self._was_ready:
                self._was_ready = True
                self._dispatch(self._on_ready, models)
            elif models is None and self._was_ready:
                self._was_ready = False
                self._dispatch(self._on_lost)
            self._stop.wait(self._poll_interval)
```

- [ ] **Step 2: Run all tests**

```bash
PYTHONPATH=app pytest tests/ -v
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add app/health_worker.py
git commit -m "refactor: health_worker — accept dispatch_fn, remove GLib dependency"
```

---

## Task 9: Refactor server_manager.py — remove idle_add_once

**Files:**
- Modify: `app/server_manager.py`

`ServerManager` calls `idle_add_once` in multiple places. Since AppController
wraps all callbacks with `dispatch_fn` before passing them to `launch()`, the
manager just calls the callbacks directly — the dispatch is already baked in.

- [ ] **Step 1: Remove all worker imports from server_manager.py**

Find every occurrence of `from worker import idle_add_once` and `idle_add_once(` in `app/server_manager.py` and replace:

```python
# Remove this import (line ~20):
from worker import idle_add_once

# Replace every:  idle_add_once(fn, *args)
# With:           fn(*args)
```

Specifically, these are the call sites to change (search for `idle_add_once` in server_manager.py):

```python
# In launch(): change
idle_add_once(on_log_line, f"$ {' '.join(cmd)}")
idle_add_once(on_log_line, f"  cwd: {config.repo_path}")
# to:
on_log_line(f"$ {' '.join(cmd)}")
on_log_line(f"  cwd: {config.repo_path}")

# In _stderr_reader(): change
idle_add_once(on_log_line, f"[stderr] {line}")
# to:
on_log_line(f"[stderr] {line}")

# In _check_line(): change
idle_add_once(on_log_line, f"⚠ Image not found: ...")
idle_add_once(on_log_line, "↺ Restarting launch...")
idle_add_once(self._on_state_cb, ServerState.ERROR)
# to direct calls (the callbacks are already wrapped by AppController)
on_log_line(f"⚠ Image not found: ...")
on_log_line("↺ Restarting launch...")
self._on_state_cb(ServerState.ERROR)

# All remaining idle_add_once calls follow the same pattern.
```

After the change, `server_manager.py` must have zero references to `idle_add_once` or `worker`.

- [ ] **Step 2: Verify no worker import remains**

```bash
grep -n "idle_add_once\|from worker" app/server_manager.py
```

Expected: no output.

- [ ] **Step 3: Run all tests**

```bash
PYTHONPATH=app pytest tests/ -v
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add app/server_manager.py
git commit -m "refactor: server_manager — remove idle_add_once, call callbacks directly"
```

---

## Task 10: Update main.py — inject dispatch and create AppController

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Update App.do_activate in app/main.py**

Replace the existing `do_activate` method:

```python
def do_activate(self):
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib

    # Bootstrap timing data on first run
    from pathlib import Path
    from timing_store import TimingStore
    timing_path = Path.home() / ".config" / "tt-runner-gui" / "timing.json"
    if not timing_path.exists():
        TimingStore(timing_path)

    # Set the module-level dispatch shim for any legacy callers still using worker.idle_add_once
    import worker
    worker.set_dispatch(GLib.idle_add)

    # Build controller with GTK dispatch
    from controller import AppController
    controller = AppController(dispatch_fn=GLib.idle_add)

    from main_window import MainWindow
    win = MainWindow(controller=controller, application=self)

    provider = Gtk.CssProvider()
    provider.load_from_data(_CSS)
    Gtk.StyleContext.add_provider_for_display(
        win.get_display(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )

    win.present()
```

- [ ] **Step 2: Run the app to sanity-check (or run tests)**

```bash
PYTHONPATH=app pytest tests/ -v
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add app/main.py
git commit -m "refactor: main.py — inject GLib.idle_add dispatch into AppController"
```

---

## Task 11: Refactor MainWindow into a thin view

**Files:**
- Modify: `app/main_window.py` — `MainWindow` class only; `Sidebar` and `MainPanel` classes are **unchanged**

The `MainWindow` class currently holds all the business logic (lines 733–1140). We replace it with a thin view that:
1. Accepts `AppController` in `__init__`
2. Registers all `on_*` callbacks
3. Delegates all actions to the controller

The `Sidebar`, `MainPanel`, `_STATE_LABELS`, `_STAGE_LABELS`, `_VLLM_STAGES`, `_MEDIA_STAGES`, `_TOUR_CARDS`, `_LOG_COLORS`, `_TYPE_ORDER`, `_TYPE_LABEL`, `_DEVICE_ORDER` definitions at the top of the file are all **kept unchanged**.

- [ ] **Step 1: Delete the old MainWindow class body and replace it**

Delete lines 733–1140 of `app/main_window.py` (the entire `MainWindow` class) and replace with:

```python
class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, controller, **kwargs):
        super().__init__(
            title="TT Model Runner",
            default_width=_settings.window_width,
            default_height=_settings.window_height,
            **kwargs,
        )
        self._ctrl = controller

        # Register all callbacks before building UI so no race on early events
        controller.on_state_changed  = self._on_state_changed
        controller.on_log_line       = self._panel_append_log   # wired after _build
        controller.on_progress       = self._on_progress
        controller.on_substage       = self._on_substage
        controller.on_catalog_loaded = self._on_catalog_loaded
        controller.on_cache_scanned  = lambda info: None       # tour managed by controller
        controller.on_bench_progress = self._panel_append_log
        controller.on_bench_result   = lambda r: None          # Plan 2
        controller.on_tool_result    = lambda r: None          # Plan 2

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(_settings.sidebar_width)

        self._sidebar = Sidebar(
            on_launch=self._on_launch_clicked,
            on_stop=lambda: self._ctrl.stop(),
            on_model_select=self._on_model_select,
            on_device_select=lambda d: None,
            on_repo_change=self._on_repo_change,
        )
        paned.set_start_child(self._sidebar)
        paned.set_resize_start_child(False)

        self._panel = MainPanel()
        paned.set_end_child(self._panel)
        self.set_child(paned)
        self.connect("close-request", self._on_close)

        # Now wire log callback (panel exists)
        controller.on_log_line = self._panel.append_log

        # Auto-discover and load repo
        from pathlib import Path
        repo_path = None
        saved = _settings.server_repo_path
        if saved:
            p = Path(saved)
            if (p / "run.py").exists() and (p / "model_spec.json").exists():
                repo_path = p
        if not repo_path:
            for c in [Path.home() / "code" / "tt-inference-server",
                      Path.home() / "tt-inference-server"]:
                if (c / "run.py").exists() and (c / "model_spec.json").exists():
                    repo_path = c
                    break
        if repo_path:
            self._sidebar._repo_entry.set_text(str(repo_path))
            GLib.idle_add(self._ctrl.load_repo, repo_path)

    # ── Callback handlers ────────────────────────────────────────────────────

    def _panel_append_log(self, line: str) -> None:
        self._panel.append_log(line)

    def _on_state_changed(self, state, info: str) -> None:
        self._panel.set_state(state, info)
        self._sidebar.set_locked(state not in (ServerState.IDLE, ServerState.ERROR))

        if state in (ServerState.IDLE, ServerState.ERROR):
            entry = self._ctrl.current_entry
            if entry:
                self._panel.show_config(entry, self._on_options_changed)
            else:
                self._panel.show_welcome()
        elif state == ServerState.LAUNCHING:
            self._panel.show_logs()

    def _on_progress(self, fraction: float, label: str) -> None:
        if fraction < 0:
            self._panel._progress_bar.pulse()
        else:
            self._panel.set_progress(fraction, label)

    def _on_substage(self, stepper: str, tour_left: str,
                     tour_right: str, dots: str) -> None:
        self._panel.set_stepper(stepper)
        self._panel.set_tour(tour_left, tour_right)
        # Patch dots label directly (MainPanel exposes update_tour_dots for int idx;
        # we bypass it here and set text directly since controller owns the index)
        self._panel._tour_dots.set_text(dots)

    def _on_catalog_loaded(self, catalog, compatible: list) -> None:
        self._sidebar.load_catalog(catalog, compatible)

    # ── Action handlers (called by Sidebar) ──────────────────────────────────

    def _on_launch_clicked(self, entry, port: str) -> None:
        options = self._panel.get_options()
        self._ctrl.launch(entry, port, options)

    def _on_model_select(self, entry) -> None:
        self._ctrl.select_model(entry)
        self._panel._banner_info.set_text(
            f"{entry.display_name}  ·  {entry.device_type}"
            f"  ·  {entry.inference_engine}"
        )
        self._panel.show_config(entry, self._on_options_changed)

    def _on_options_changed(self, options) -> None:
        self._ctrl.set_options(options)

    def _on_repo_change(self, path) -> None:
        self._ctrl.load_repo(path)

    def _on_close(self, win) -> bool:
        _settings.window_width = self.get_width()
        _settings.window_height = self.get_height()
        _settings.save()
        if self._ctrl.state not in (ServerState.IDLE, ServerState.ERROR):
            self._ctrl.stop()
        return False
```

- [ ] **Step 2: Remove now-unused imports from top of main_window.py**

The old `MainWindow` imported `GLib` for timers and idle_add. Check the top of the file — keep only what `Sidebar` and `MainPanel` still need. The new `MainWindow` uses `GLib.idle_add` once for repo loading, so the `GLib` import stays. Remove any `from server_manager import LaunchConfig` if it's no longer directly referenced (it's now only used in `controller.py`).

```bash
grep -n "^from\|^import" app/main_window.py
```

Remove any import only used in the deleted business logic. Keep: `gi`, `GLib`, `Gtk`, `Pango`, `threading`, `time`, `Path`, `Optional`, `List`, `_settings`, `detect_devices`, `ModelCacheInfo`, `scan_model_cache`, `ModelCatalog`, `ModelEntry`, `ServerManager`, `ServerState`, `HealthWorker`, `TimingStore`, `idle_add_once`. Actually most of these are now only in controller.py — remove from main_window.py anything not referenced by `Sidebar`, `MainPanel`, or the new thin `MainWindow`.

The safe minimal set for main_window.py after refactor:
```python
import os
import threading
import time
from pathlib import Path
from typing import List, Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import GLib, Gtk, Pango

from app_settings import settings as _settings
from model_catalog import ModelCatalog, ModelEntry
from server_manager import ServerState
```

- [ ] **Step 3: Create stub tui_main.py**

```python
# app/tui_main.py
# SPDX-License-Identifier: Apache-2.0
"""TUI entry point (Plan 2). Stub so ./run --tui fails gracefully."""


def main():
    raise NotImplementedError(
        "TUI not yet implemented — run Plan 2 to build app/tui/"
    )
```

- [ ] **Step 4: Update ./run to accept --tui flag**

Read the current `run` script, then replace with:

```bash
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$SCRIPT_DIR/app"

if [[ "${1:-}" == "--tui" ]]; then
    exec python3 "$SCRIPT_DIR/app/tui_main.py" "${@:2}"
else
    exec python3 "$SCRIPT_DIR/app/main.py" "$@"
fi
```

```bash
chmod +x run
```

- [ ] **Step 5: Run full test suite**

```bash
PYTHONPATH=app pytest tests/ -v
```

Expected: all tests pass. If any existing test breaks due to the MainWindow refactor, the test was testing the old business logic that now lives in AppController — update it to test via AppController directly.

- [ ] **Step 6: Commit everything**

```bash
git add app/main_window.py app/tui_main.py run
git commit -m "refactor: MainWindow → thin GTK view over AppController; add --tui stub"
```

---

## Task 12: Final integration check + run the app

- [ ] **Step 1: Run complete test suite**

```bash
PYTHONPATH=app pytest tests/ -v --tb=short 2>&1 | tail -20
```

Expected: all tests green, including existing `test_server_manager`, `test_model_catalog`, `test_device_detector`, etc.

- [ ] **Step 2: Verify no GLib import in non-GUI modules**

```bash
grep -rn "from gi\|import gi\|GLib\|Gtk" \
  app/controller.py app/server_manager.py \
  app/health_worker.py app/worker.py
```

Expected: zero matches. (Only `main.py`, `main_window.py`, `config_panel.py` should still have GTK imports.)

- [ ] **Step 3: Commit final state**

```bash
git add -A
git commit -m "feat: Plan 1 complete — AppController extracted, GTK decoupled, dispatch abstracted"
```

---

## What's next

**Plan 2** (write separately once this plan is merged):

1. **Textual TUI** — `app/tui/app.py`, `screens.py`, five pane widgets
2. **Tool calling** — `app/tool_client.py` + Tools tab in both UIs
3. **Benchmark integration** — `app/benchmark_runner.py` + Bench tab in both UIs

All three build directly on `AppController` — no further changes to the core needed.
