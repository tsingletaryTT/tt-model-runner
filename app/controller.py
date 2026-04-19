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
