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
import json
import os
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from app_settings import settings as _settings
from compat_catalog import CompatCatalog, load_async as _compat_load_async
from dev_image_launcher import DevImageLauncher, DevLaunchConfig
from device_detector import ChipStatus, detect_devices, get_chip_statuses_live
from health_worker import HealthWorker
from hf_cache import ModelCacheInfo, scan_model_cache
from launch_options import LaunchOptions
from model_catalog import ModelCatalog, ModelEntry
from server_manager import LaunchConfig, ServerManager, ServerState
from timing_store import TimingStore


@dataclass
class RunningServer:
    """A TT inference server container found via docker ps."""
    container_name: str
    image: str
    port: str          # e.g. "8000"
    running_for: str   # human-readable uptime, e.g. "7 hours ago"
    state: str         # "running" | ...


def _parse_running_servers() -> "List[RunningServer]":
    """Scan docker ps for containers that look like TT inference servers.

    Matches on container name prefix 'tt-inference-server' OR image containing
    'tenstorrent' (covers both tt-media-inference-server and tt-vllm variants).
    Returns a list of RunningServer instances (may be empty).
    """
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{json .}}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    servers = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = rec.get("Names", "")
        image = rec.get("Image", "")
        state = rec.get("State", "")
        if not (name.startswith("tt-inference-server") or "tenstorrent" in image.lower()):
            continue
        ports_str = rec.get("Ports", "")
        # Parse first host port from e.g. "0.0.0.0:8000->8000/tcp"
        port = ""
        import re as _re
        m = _re.search(r':(\d+)->', ports_str)
        if m:
            port = m.group(1)
        servers.append(RunningServer(
            container_name=name,
            image=image,
            port=port,
            running_for=rec.get("RunningFor", rec.get("Status", "unknown")),
            state=state,
        ))
    return servers


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
        self._last_error_hint: str = ""   # last error-ish log line, shown in ERROR banner
        self._compat_catalog: Optional[CompatCatalog] = None
        self._dev_launcher = DevImageLauncher()
        self._pull_layers_done: int = 0   # layers that reached "Pull complete"
        self._pull_downloading: dict = {}  # layer_id → (current_bytes, total_bytes)
        self._emitted_error_hints: set = set()  # patterns already suggested this run
        self._hw_poll_timer: Optional[threading.Timer] = None
        self._hw_poll_active: bool = False  # set True after first repo load

        # Fetch compatibility catalog in the background — dispatches on_compat_catalog_loaded.
        def _on_compat(cat: Optional[CompatCatalog]) -> None:
            self._compat_catalog = cat
            self._emit("on_compat_catalog_loaded", cat)
        _compat_load_async(_on_compat)

        # Callbacks — views set these after construction; None = ignored
        self.on_state_changed: Optional[Callable] = None        # (ServerState, str)
        self.on_log_line: Optional[Callable] = None              # (str,)
        self.on_progress: Optional[Callable] = None              # (float, str)
        self.on_substage: Optional[Callable] = None              # (str, str, str, str)
        self.on_catalog_loaded: Optional[Callable] = None        # (ModelCatalog, List[str])
        self.on_cache_scanned: Optional[Callable] = None         # (ModelCacheInfo,)
        self.on_bench_progress: Optional[Callable] = None        # (str,)
        self.on_bench_result: Optional[Callable] = None          # (BenchResult,)
        self.on_tool_result: Optional[Callable] = None           # (ToolRoundTrip,)
        self.on_running_servers: Optional[Callable] = None           # (List[RunningServer],)
        self.on_hardware_status: Optional[Callable] = None           # (List[ChipStatus],)
        self.on_compat_catalog_loaded: Optional[Callable] = None     # (Optional[CompatCatalog],)

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

    @property
    def compat_catalog(self) -> Optional[CompatCatalog]:
        return self._compat_catalog

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
            # Emit live chip telemetry for the hardware status widget.
            chips = get_chip_statuses_live()
            if chips:
                self._emit("on_hardware_status", chips)
            # Scan for already-running servers now that we have device/catalog context.
            servers = _parse_running_servers()
            if servers:
                self._emit("on_running_servers", servers)
            # Start the 30-second periodic hardware poll.
            self._hw_poll_active = True
            self._schedule_hw_poll()

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

    @staticmethod
    def _is_port_open(port: str) -> bool:
        """Return True if something is already listening on localhost:port."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                return s.connect_ex(("127.0.0.1", int(port))) == 0
        except (OSError, ValueError):
            return False

    def launch(self, entry: ModelEntry, port: str,
               options: Optional[LaunchOptions] = None) -> None:
        """Start the inference server for the given model entry.

        Pre-flight: if the target port is already bound by a known TT inference
        container, emits on_running_servers instead of starting a duplicate.
        If it's bound by an unknown process, logs a warning and tries to launch
        anyway (docker run will surface the conflict in its own error output).
        """
        if self._state not in (ServerState.IDLE, ServerState.ERROR):
            return
        if options:
            self._options = options

        # Run pre-flight checks + actual launch in a background thread so we
        # never block the UI for the docker-ps / stat calls.
        def _preflight_and_launch():
            # Disk space check: warn if available space is less than model's requirement.
            if entry.min_disk_gb:
                try:
                    import shutil
                    cache_dir = Path.home() / ".cache" / "huggingface"
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    free_gb = shutil.disk_usage(cache_dir).free / 1e9
                    if free_gb < entry.min_disk_gb:
                        self._emit("on_log_line",
                                   f"⚠ Low disk space: {free_gb:.1f} GB free, "
                                   f"{entry.min_disk_gb:.0f} GB needed for {entry.display_name} — "
                                   f"launch may fail or be very slow")
                except OSError:
                    pass

            if self._is_port_open(port):
                # Something is listening — check if it's one of our containers.
                servers = _parse_running_servers()
                matching = [s for s in servers if s.port == str(port)]
                if matching:
                    # Hand off to the reconnect path via the existing callback.
                    self._emit("on_log_line",
                               f"⚠ Port {port} already used by {matching[0].container_name} — "
                               f"reconnect instead of launching a new server")
                    self._emit("on_running_servers", matching)
                    return
                # Unknown process — warn but continue; docker run will fail loudly.
                self._emit("on_log_line",
                           f"⚠ Port {port} is already in use by an unknown process — "
                           f"launch may fail")
            self._do_launch(entry, port)

        threading.Thread(target=_preflight_and_launch, daemon=True).start()

    def _do_launch(self, entry: ModelEntry, port: str) -> None:
        """Internal: perform the actual server launch (called after port pre-flight)."""
        self._port = port
        self._current_entry = entry

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

        # Record in recent_models (newest first, capped at 5 unique entries)
        rec = {"model_name": entry.model_name, "device": entry.device_type,
               "display": entry.display_name}
        recents = [r for r in (_settings.recent_models or [])
                   if r.get("model_name") != rec["model_name"] or r.get("device") != rec["device"]]
        recents.insert(0, rec)
        _settings.recent_models = recents[:5]
        _settings.save()

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
        """Stop the running server or dev-image script."""
        self._transition(ServerState.STOPPING)
        if self._health_worker:
            self._health_worker.stop()
            self._health_worker = None
        self._server_mgr.stop()
        self._dev_launcher.stop()
        t = threading.Timer(10.0, self._force_idle)
        t.daemon = True
        t.start()

    def restart(self) -> None:
        """Stop the current server then relaunch with the same model and options.

        Only valid when a model has been launched before (current_entry is set).
        Skips the port pre-flight check since we own the container.
        """
        entry = self._current_entry
        port = self._port
        if not entry or not port:
            self._emit("on_log_line", "⚠ Restart: no previous launch to replay")
            return

        self._emit("on_log_line", f"↺ Restarting {entry.display_name}…")

        def _after_stop():
            # Wait for state to settle then relaunch directly (no pre-flight)
            import time as _time
            deadline = _time.monotonic() + 15.0
            while _time.monotonic() < deadline:
                if self._state in (ServerState.IDLE, ServerState.ERROR):
                    break
                _time.sleep(0.5)
            self._do_launch(entry, port)

        # Stop first (async); then relaunch from the stop-wait thread
        self._transition(ServerState.STOPPING)
        if self._health_worker:
            self._health_worker.stop()
            self._health_worker = None
        self._server_mgr.stop()
        self._dev_launcher.stop()
        threading.Thread(target=_after_stop, daemon=True).start()

    def _force_idle(self) -> None:
        """Fallback: transition to IDLE if still stuck in STOPPING after timeout."""
        if self._state == ServerState.STOPPING:
            self._transition(ServerState.IDLE)

    # ── Hardware utilities ────────────────────────────────────────────────────

    def _rebooted_since_last_launch(self) -> bool:
        """Return True if the machine was rebooted after the last launch.

        A reboot leaves the TT devices in a clean state so tt-smi -r is not
        needed.  Suspends/sleeps do NOT reset the devices — only a full reboot
        does.  We detect this by comparing the persisted launch timestamp against
        the current system boot time read from /proc/uptime.
        """
        last_at = _settings.last_launched_at
        if not last_at:
            return True  # no history → assume clean
        try:
            uptime_secs = float(Path("/proc/uptime").read_text().split()[0])
            boot_time = time.time() - uptime_secs
            return boot_time > last_at
        except OSError:
            return False  # can't tell → be conservative (warn)

    def needs_reset_warning(self, entry: ModelEntry) -> Optional[tuple]:
        """Return (old_engine, new_engine, old_display) if engines differ, else None.

        Returns None when a reboot has occurred since the last launch — the
        devices are already in a clean state and tt-smi -r is unnecessary.
        """
        if self._rebooted_since_last_launch():
            return None  # clean boot, no reset needed
        last_engine = _settings.last_launched_engine
        if last_engine and last_engine != entry.inference_engine:
            old_display = _settings.last_launched_model_display or "previous model"
            return (last_engine, entry.inference_engine, old_display)
        return None

    def reset_hardware(self, on_complete: Optional[Callable[[bool], None]] = None) -> None:
        """Run tt-smi -r in a background thread, streaming output via on_log_line.

        on_complete(success) is dispatched on the UI event loop when the
        subprocess exits.  success is True iff returncode == 0.
        """
        def _run() -> None:
            self._emit("on_log_line", "⟳ Resetting TT hardware (tt-smi -r)…")
            success = False
            try:
                proc = subprocess.Popen(
                    ["tt-smi", "-r"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                for line in proc.stdout:
                    self._emit("on_log_line", line.rstrip("\n"))
                rc = proc.wait()
                if rc == 0:
                    self._emit("on_log_line", "✓ Hardware reset complete")
                    success = True
                else:
                    self._emit("on_log_line", f"✗ tt-smi -r exited with code {rc}")
            except FileNotFoundError:
                self._emit("on_log_line", "✗ tt-smi not found — is it on PATH?")
            except Exception as exc:
                self._emit("on_log_line", f"✗ tt-smi -r error: {exc}")
            if on_complete:
                self._dispatch(on_complete, success)

        threading.Thread(target=_run, daemon=True).start()

    def get_repo_git_info(self, path: Optional[Path] = None) -> tuple:
        """Return (branch, short_hash) for the server repo, or ('', '') on failure."""
        repo = path or Path(_settings.server_repo_path)
        try:
            branch = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
                stderr=subprocess.DEVNULL, text=True,
            ).strip()
            sha = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                stderr=subprocess.DEVNULL, text=True,
            ).strip()
            return (branch, sha)
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            return ("", "")

    def pull_repo(self, on_complete: Optional[Callable[[bool, str], None]] = None) -> None:
        """Run 'git pull' on the configured server repo in a background thread.

        Streams output via on_log_line.
        on_complete(success, summary) is dispatched on the UI event loop when done.
        """
        repo_path = Path(_settings.server_repo_path)

        def _run() -> None:
            self._emit("on_log_line", f"⟳ git pull in {repo_path}…")
            success, summary = False, ""
            try:
                proc = subprocess.Popen(
                    ["git", "-C", str(repo_path), "pull"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                )
                lines = []
                for line in proc.stdout:
                    stripped = line.rstrip("\n")
                    self._emit("on_log_line", stripped)
                    lines.append(stripped)
                rc = proc.wait()
                if rc == 0:
                    summary = lines[-1] if lines else "Already up to date."
                    self._emit("on_log_line", f"✓ git pull complete: {summary}")
                    success = True
                else:
                    summary = f"exit code {rc}"
                    self._emit("on_log_line", f"✗ git pull exited with code {rc}")
            except FileNotFoundError:
                summary = "git not found"
                self._emit("on_log_line", "✗ git not found — is it on PATH?")
            except Exception as exc:
                summary = str(exc)
                self._emit("on_log_line", f"✗ git pull error: {exc}")
            if on_complete:
                self._dispatch(on_complete, success, summary)

        threading.Thread(target=_run, daemon=True).start()

    def refresh_hardware_status(self) -> None:
        """Re-run tt-smi -s in a background thread and emit on_hardware_status."""
        def _run():
            chips = get_chip_statuses_live()
            self._emit("on_hardware_status", chips)
        threading.Thread(target=_run, daemon=True).start()

    _HW_POLL_INTERVAL_S: int = 30

    def _schedule_hw_poll(self) -> None:
        """Schedule next periodic hardware telemetry poll (30 s)."""
        if not self._hw_poll_active:
            return
        self._hw_poll_timer = threading.Timer(self._HW_POLL_INTERVAL_S, self._hw_poll_tick)
        self._hw_poll_timer.daemon = True
        self._hw_poll_timer.start()

    def _hw_poll_tick(self) -> None:
        chips = get_chip_statuses_live()
        self._emit("on_hardware_status", chips)
        self._schedule_hw_poll()

    def scan_running_servers(self) -> None:
        """Scan for already-running TT inference containers in a background thread.

        Emits on_running_servers(List[RunningServer]) on the UI event loop.
        Called automatically at startup after the repo is loaded.
        """
        def _run():
            servers = _parse_running_servers()
            if servers:
                self._emit("on_running_servers", servers)
        threading.Thread(target=_run, daemon=True).start()

    def adopt_running_server(self, port: str, container_name: str = "") -> None:
        """Reconnect to an already-running inference server without launching a new one.

        Transitions directly to LOADING state and starts the health worker so
        the app reaches READY as soon as the health endpoint responds.
        """
        if self._state not in (ServerState.IDLE, ServerState.ERROR):
            return
        self._port = port
        _settings.last_port = port
        _settings.save()
        if container_name:
            self._server_mgr._container_name = container_name
        banner = f"Reconnecting to existing server on port {port}"
        if container_name:
            banner += f" ({container_name})"
        self._emit("on_log_line", f"⟳ {banner}")
        self._transition(ServerState.LOADING)
        self._health_worker = HealthWorker(
            port=port,
            on_ready=self._on_health_ready,
            on_lost=self._on_health_lost,
            dispatch_fn=self._dispatch,
            engine="auto",   # tries /v1/models then /tt-liveness
        )
        self._health_worker.start()

    def launch_dev_image(self, model_id: str, software_stack: str) -> None:
        """Launch a model script via the tt-developer-image Docker container.

        model_id: compatibility.json entry id (e.g. "bge-large-en-v1-5")
        software_stack: "tt-forge" | "tt-metal" | "tt-vllm"

        State machine: IDLE/ERROR/DONE → LAUNCHING → RUNNING → DONE or ERROR.
        """
        if self._state not in (ServerState.IDLE, ServerState.ERROR, ServerState.DONE):
            return
        dev_repo = Path(_settings.dev_image_repo_path)
        config = DevLaunchConfig(
            dev_image_repo=dev_repo,
            model_id=model_id,
            software_stack=software_stack,
        )
        self._emit("on_log_line",
                   f"▶ Dev image launch: {model_id} via {software_stack}")
        self._transition(ServerState.LAUNCHING)
        self._dev_launcher.launch(config, self._handle_log_line, self._on_server_state)

    def _handle_log_line(self, line: str) -> None:
        """Forward a log line to views and update state/substage from it."""
        self._emit("on_log_line", line)
        # Keep the last actionable error message for the ERROR state banner.
        if re.search(r'ERROR|failed|✗|⚠.*[Cc]ontainer|exit.*code\s*[1-9]', line):
            stripped = line.strip()
            if stripped and not re.search(r'error_handler|on_error|ErrorHandler', stripped):
                self._last_error_hint = stripped[:120]
        # Emit a recovery suggestion for well-known error patterns (once per pattern per run).
        for pattern, hint in self._ERROR_HINTS:
            pid = id(pattern)
            if pid not in self._emitted_error_hints and pattern.search(line):
                self._emitted_error_hints.add(pid)
                self._emit("on_log_line", f"💡 {hint}")
                break
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

        elif self._state == ServerState.PULLING_IMAGE:
            pull_summary = self._update_pull_progress(line)
            if pull_summary:
                self._emit("on_substage", "⬇", pull_summary, "", "")
                # Emit a fractional progress when we have download size data.
                active = list(self._pull_downloading.values())
                if active:
                    size_total = sum(t for _, t in active)
                    cur_total  = sum(c for c, _ in active)
                    if size_total > 0:
                        frac = min(cur_total / size_total * 0.85, 0.85)
                        self._emit("on_progress", frac, pull_summary)

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
        elif state == ServerState.ERROR and self._last_error_hint:
            # Surface the last error line so the user doesn't have to scroll logs.
            info = self._last_error_hint
        elif self._current_entry:
            port = _settings.last_port
            info = (f"localhost:{port}  ·  {self._current_entry.display_name}"
                    f"  ·  {self._current_entry.device_type}")
        else:
            info = ""

        self._emit("on_state_changed", state, info)

        if state == ServerState.PULLING_IMAGE:
            self._pull_layers_done = 0
            self._pull_downloading.clear()
        elif state == ServerState.LAUNCHING:
            # Persist launch metadata for cross-engine reset warnings and reboot detection.
            self._last_error_hint = ""   # fresh slate for the new run
            self._emitted_error_hints.clear()
            if self._current_entry:
                _settings.last_launched_engine = self._current_entry.inference_engine
                _settings.last_launched_model_display = self._current_entry.display_name
            _settings.last_launched_at = time.time()
            _settings.save()
        elif state == ServerState.LOADING:
            self._load_start = time.monotonic()
            self._tour_card_idx = 0
            self._tour_substage = None
            self._start_progress_ticker()
            self._start_tour_timer()
        elif state in (ServerState.READY, ServerState.ERROR,
                       ServerState.IDLE, ServerState.STOPPING, ServerState.DONE):
            self._stop_progress_ticker()
            self._stop_tour_timer()
            if state == ServerState.ERROR:
                # Kill monitoring threads so ghost log lines don't appear after
                # the failure.  _server_mgr.stop() sets _stop_event (silencing
                # the tail threads) and sends docker stop (no-op if already gone).
                if self._health_worker:
                    self._health_worker.stop()
                    self._health_worker = None
                self._server_mgr.stop()

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

    # Error recovery hints ──────────────────────────────────────────────────────

    # Each tuple: (compiled pattern, suggestion string).
    # Matched against raw log lines; first match wins per ERROR transition.
    _ERROR_HINTS: list = [
        (re.compile(r'Cannot connect to the Docker daemon', re.I),
         "Docker is not running. Start it: sudo systemctl start docker"),
        (re.compile(r'permission denied.*docker', re.I),
         "Add your user to the docker group: sudo usermod -aG docker $USER  (then log out/in)"),
        (re.compile(r'no space left on device', re.I),
         "Disk full — free up space or point HF_HOME to a larger volume"),
        (re.compile(r'pull access denied|unauthorized: authentication', re.I),
         "Docker image access denied. Try: docker login ghcr.io"),
        (re.compile(r'/dev/tenstorrent.*No such file|device not found', re.I),
         "TT device not found — check: ls /dev/tenstorrent* and ensure drivers are loaded"),
        (re.compile(r'tt.smi|firmware|device reset', re.I),
         "TT device error — try resetting: tt-smi -r"),
        (re.compile(r'401.*Unauthorized|HF_TOKEN.*not found|Token.*invalid', re.I),
         "HuggingFace token missing or invalid — set HF_TOKEN in your .env or environment"),
        (re.compile(r'model_spec\.json.*not found|model_spec.*missing', re.I),
         "model_spec.json not found — verify the server repo path in Settings"),
        (re.compile(r'max_model_len|context_length.*exceed|KV cache.*full', re.I),
         "Context too large — reduce max_model_len in the Config panel Quick Settings"),
        (re.compile(r'out of memory|ENOMEM|cannot allocate', re.I),
         "Out of memory — try a smaller model, reduce context, or reset the TT device: tt-smi -r"),
        (re.compile(r'exec format error|wrong ELF class', re.I),
         "Binary architecture mismatch — try pulling the image again: docker pull <image>"),
        (re.compile(r'connection refused.*health|health.*refused', re.I),
         "Server did not respond to health check — check log for earlier startup errors"),
    ]
    _emitted_error_hints: set  # instance var, init in __init__

    # Docker pull progress ──────────────────────────────────────────────────────

    _RE_PULL_LAYER = re.compile(
        r'^([a-f0-9]{12}):\s+Downloading\s.*?(\d+(?:\.\d+)?)\s*(kB|MB|GB)\s*/\s*(\d+(?:\.\d+)?)\s*(kB|MB|GB)',
        re.I,
    )
    _RE_PULL_DONE  = re.compile(r'Pull complete', re.I)
    _SCALE = {"kb": 1e3, "mb": 1e6, "gb": 1e9}

    def _update_pull_progress(self, line: str) -> str:
        """Parse a Docker pull log line, update counters, return a progress summary or ''."""
        if self._RE_PULL_DONE.search(line):
            self._pull_layers_done += 1
        m = self._RE_PULL_LAYER.match(line)
        if m:
            lid, cur_v, cur_u, tot_v, tot_u = m.groups()
            cur_bytes = float(cur_v) * self._SCALE.get(cur_u.lower(), 1)
            tot_bytes = float(tot_v) * self._SCALE.get(tot_u.lower(), 1)
            self._pull_downloading[lid] = (cur_bytes, tot_bytes)
        if not (self._pull_layers_done or self._pull_downloading):
            return ""
        active = list(self._pull_downloading.values())
        cur_total = sum(c for c, _ in active)
        size_total = sum(t for _, t in active)

        def _fmt(b: float) -> str:
            if b >= 1e9:
                return f"{b/1e9:.1f} GB"
            if b >= 1e6:
                return f"{b/1e6:.0f} MB"
            return f"{b/1e3:.0f} kB"

        parts = []
        if self._pull_layers_done:
            parts.append(f"{self._pull_layers_done} layers done")
        if active:
            pct = int(100 * cur_total / size_total) if size_total else 0
            parts.append(f"downloading {_fmt(cur_total)} / {_fmt(size_total)} ({pct}%)")
        return "  ·  ".join(parts) if parts else ""

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

    def run_benchmark(
        self,
        mode: str = "smoke-test",
        concurrency_sweeps: bool = False,
        percentile_report: bool = False,
    ) -> None:
        """Run tt-inference-server benchmarks for the current model.

        Spawns BenchmarkRunner in a background thread. Emits on_bench_progress
        for each log line and on_bench_result for each parsed result file.

        Args:
            mode: Sampling mode passed to run.py as --limit-samples-mode
                  (e.g. "smoke-test", "full").
            concurrency_sweeps: If True, sweeps across concurrency levels.
            percentile_report: If True, requests percentile breakdown in output.
        """
        from benchmark_runner import BenchmarkRunner

        repo_path = Path(_settings.server_repo_path)
        if not (repo_path / "run.py").exists():
            self._emit("on_bench_progress",
                       f"⚠ run.py not found at {repo_path} — cannot run benchmark")
            return

        model_name = (self._current_entry.display_name
                      if self._current_entry else "unknown")
        device = (self._current_entry.device_type
                  if self._current_entry else "unknown")

        # Pull perf_reference targets from model_spec.json when available so
        # BenchmarkRunner can evaluate pass/fail without a second disk read.
        perf_targets: dict = {}
        spec_path = repo_path / "model_spec.json"
        if self._current_entry and spec_path.exists():
            try:
                spec_data = json.loads(spec_path.read_text())
                model_key = self._current_entry.model_name
                device_key = self._current_entry.device_type
                impl_data = (spec_data.get("model_specs", {})
                             .get(model_key, {}).get(device_key, {}))
                for engine_map in impl_data.values():
                    if isinstance(engine_map, dict):
                        for impl in engine_map.values():
                            if isinstance(impl, dict) and "perf_reference" in impl:
                                refs = impl["perf_reference"]
                                if refs:
                                    perf_targets = refs[0].get("targets", {})
                                break
                        break
            except Exception:
                # Best-effort — if parsing fails, run without targets
                pass

        def _on_result(r):
            self._persist_bench_result(r)
            self._emit("on_bench_result", r)

        runner = BenchmarkRunner(
            repo_path=repo_path,
            on_progress=lambda line: self._emit("on_bench_progress", line),
            on_result=_on_result,
        )
        runner.run(
            model_name=model_name,
            device=device,
            mode=mode,
            concurrency_sweeps=concurrency_sweeps,
            percentile_report=percentile_report,
            perf_targets=perf_targets,
        )

    def _persist_bench_result(self, r) -> None:
        """Append a BenchResult to settings.benchmark_history (max 50 entries)."""
        entry = {
            "model_name": r.model_name,
            "device": r.device,
            "timestamp": r.timestamp,
            "isl": r.isl,
            "osl": r.osl,
            "concurrency": r.concurrency,
            "mean_ttft_ms": r.mean_ttft_ms,
            "p95_ttft_ms": r.p95_ttft_ms,
            "mean_tps": r.mean_tps,
            "tps_decode": r.tps_decode,
            "mean_e2el_ms": r.mean_e2el_ms,
            "request_throughput": r.request_throughput,
            "tier_pass": r.tier_pass,
        }
        history = list(_settings.benchmark_history or [])
        history.append(entry)
        _settings.benchmark_history = history[-50:]  # keep last 50
        _settings.save()

    def get_bench_history(self) -> list:
        """Return the persisted benchmark history as a list of dicts (newest first)."""
        return list(reversed(_settings.benchmark_history or []))

    def is_starred(self, entry: ModelEntry) -> bool:
        """Return True if this model/device is in the starred list."""
        starred = _settings.starred_models or []
        return any(
            s.get("model_name") == entry.model_name and s.get("device") == entry.device_type
            for s in starred
        )

    def toggle_star(self, entry: ModelEntry) -> bool:
        """Toggle the starred state for an entry. Returns new starred state."""
        starred = list(_settings.starred_models or [])
        rec = {"model_name": entry.model_name, "device": entry.device_type}
        existing = [i for i, s in enumerate(starred)
                    if s.get("model_name") == entry.model_name and s.get("device") == entry.device_type]
        if existing:
            for i in reversed(existing):
                del starred[i]
            _settings.starred_models = starred
            _settings.save()
            return False
        starred.append(rec)
        _settings.starred_models = starred
        _settings.save()
        return True

    def send_tool_call(self, tools: list, prompt: str) -> None:
        """Send a multi-turn tool-call to the running server.

        Runs in a background thread. Emits on_tool_result for each step:
          step="call"   — model requested a tool call
          step="result" — auto-generated result was injected
          step="final"  — model's final text reply (or error message)
        """
        port = self._port or "8000"
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
