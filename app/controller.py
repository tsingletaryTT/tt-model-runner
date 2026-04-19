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


@dataclass
class ToolRoundTrip:
    """One step in a multi-turn tool-call exchange emitted via on_tool_result."""
    step: str        # "call" | "result" | "final"
    name: str        # tool function name (populated for "call" step)
    arguments: str   # JSON string of arguments (populated for "call" step)
    content: str     # result content or final assistant reply


@dataclass
class BenchResult:
    """Parsed output from one benchmark configuration."""
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


from tool_client import run_session as _tc_run_session

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
        self._port: Optional[str] = None

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
        """Called when user selects a model; triggers HF cache scan."""
        self._current_entry = entry
        self._cache_info = None

        def _scan():
            info = scan_model_cache(entry.hf_model_repo)
            self._cache_info = info
            self._emit("on_cache_scanned", info)

        threading.Thread(target=_scan, daemon=True).start()

    def _read_hf_token(self, repo_path: Path) -> Optional[str]:
        """Read HF_TOKEN from environment or .env file in repo_path."""
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
        """Start the inference server for the given model entry."""
        if self._state not in (ServerState.IDLE, ServerState.ERROR):
            return
        self._port = port
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
            dispatch_fn=self._dispatch,
            engine="media" if entry.inference_engine == "media" else "vllm",
        )
        self._health_worker.start()
        self._server_mgr.launch(config, self._handle_log_line, self._on_server_state)

    def stop(self) -> None:
        """Stop the running server."""
        self._transition(ServerState.STOPPING)
        if self._health_worker:
            self._health_worker.stop()
            self._health_worker = None
        self._server_mgr.stop()
        t = threading.Timer(10.0, self._force_idle)
        t.daemon = True
        t.start()

    def _force_idle(self) -> None:
        """Fallback: transition to IDLE if still stuck in STOPPING after timeout."""
        if self._state == ServerState.STOPPING:
            self._transition(ServerState.IDLE)

    def _handle_log_line(self, line: str) -> None:
        """Forward a log line to views and update state/substage from it."""
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
        """Callback from ServerManager when a state change is detected in the log."""
        self._transition(state)

    def _on_health_ready(self, models: list) -> None:
        """Called by HealthWorker when the server's health endpoint returns 200."""
        if self._state in (ServerState.LOADING, ServerState.LAUNCHING,
                           ServerState.PULLING_IMAGE):
            mstr = ", ".join(models) if models else "ready"
            port = _settings.last_port
            self._transition(ServerState.READY,
                             info_override=f"localhost:{port}  ·  {mstr}")
            if self._load_start and self._current_entry:
                dur = time.monotonic() - self._load_start
                self._timing.record_load(
                    self._current_entry.hf_model_repo,
                    self._current_entry.device_type,
                    dur, cold=False,
                )

    def _on_health_lost(self) -> None:
        """Called by HealthWorker when health endpoint stops responding after READY."""
        if self._state == ServerState.READY:
            self._transition(ServerState.ERROR)
            self._emit("on_log_line",
                       "⚠ Health check lost — server may have crashed")

    def _transition(self, state: ServerState, info_override: str = "") -> None:
        """Transition to a new state, emit on_state_changed, and manage timers.

        Args:
            state: The new ServerState to transition to.
            info_override: When non-empty, use this string as the info payload
                for on_state_changed instead of the default
                "localhost:port · display_name · device_type" format.
                Used by _on_health_ready to surface model names without a
                second emit.
        """
        if state == self._state:
            return
        self._state = state

        if info_override:
            info = info_override
        elif self._current_entry:
            port = _settings.last_port
            info = (f"localhost:{port}  ·  {self._current_entry.display_name}"
                    f"  ·  {self._current_entry.device_type}")
        else:
            info = ""

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
        """Start the 1-second repeating timer that emits on_progress during LOADING."""
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
        """Cancel the progress ticker if it is running."""
        if self._progress_timer:
            self._progress_timer.cancel()
            self._progress_timer = None

    def _start_tour_timer(self) -> None:
        """Start the 12-second repeating timer that rotates tour cards during LOADING."""
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
        """Cancel the tour card rotation timer if it is running."""
        if self._tour_timer:
            self._tour_timer.cancel()
            self._tour_timer = None

    def _progress_tick(self) -> None:
        """Emit on_progress based on the best available progress signal."""
        if self._state != ServerState.LOADING or not self._current_entry:
            return
        elapsed = time.monotonic() - (self._load_start or time.monotonic())
        parser = self._server_mgr.parser

        # Highest priority: deterministic trace capture progress (0–10 captures)
        if parser.trace_capture_count > 0:
            frac = parser.trace_capture_count / 10.0
            remaining = (10 - parser.trace_capture_count) * 3
            self._emit("on_progress", frac,
                       f"Capturing traces {parser.trace_capture_count}/10"
                       f" · ~{remaining:.0f}s remaining")
            return

        # Next: warmup progress from WAN-style tqdm "N/total" lines
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

        # Fallback: time-based estimate from TimingStore
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
            self._emit("on_progress", -1.0, "")

    def _build_stepper_text(self, substage: str) -> str:
        """Build a pipeline stepper string marking completed (✓), active (●), and pending (○) stages."""
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
        """Build the left panel content of the tour panel: model facts and cache info."""
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
            if isinstance(entry.param_count, (int, float)) and entry.param_count:
                lines.append(f"  {entry.param_count:.0f}B parameters")
        lines.append(f"  Engine: {entry.inference_engine}")
        lines.append(f"  Status: {entry.status}")
        return "\n".join(lines)

    def _build_tour_content(self, substage: Optional[str]) -> tuple:
        """Return (left_text, right_text) for the tour panel given the current substage."""
        entry = self._current_entry
        if not entry:
            return ("", "")
        left = self._build_tour_left(entry)
        cards = TOUR_CARDS.get(substage or "", [])
        right = (cards[self._tour_card_idx % len(cards)]
                 if cards else "Loading model onto Tenstorrent hardware…")
        return (left, right)

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
        """Send a multi-turn tool-call to the running server.

        Runs in a background thread. Emits on_tool_result for each step:
          step="call"   — model requested a tool call
          step="result" — auto-generated result was injected
          step="final"  — model's final text reply (or error message)
        """
        port = getattr(self, "_port", "8000")
        base_url = f"http://localhost:{port}"
        model = (self._current_entry.hf_model_repo
                 if self._current_entry else "default")

        def _run() -> None:
            try:
                for step, payload in _tc_run_session(base_url, model, tools, prompt):
                    if step == "tool_call":
                        rt = ToolRoundTrip(
                            step="call",
                            name=payload.name,
                            arguments=payload.arguments,
                            content="",
                        )
                    elif step == "tool_result":
                        rt = ToolRoundTrip(step="result", name="", arguments="",
                                           content=payload)
                    else:
                        rt = ToolRoundTrip(step="final", name="", arguments="",
                                           content=payload)
                    self._emit("on_tool_result", rt)
            except Exception as exc:
                self._emit("on_tool_result",
                           ToolRoundTrip(step="final", name="", arguments="",
                                         content=f"Error: {exc}"))

        threading.Thread(target=_run, daemon=True).start()
