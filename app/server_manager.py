#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Launch run.py, tail its log file, stop container.

Callbacks (on_log_line, on_state) are called directly; callers are
responsible for any thread-dispatching they need (e.g. wrapping with
GLib.idle_add before passing to ServerManager.launch).
"""
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

log = logging.getLogger(__name__)
from typing import Callable, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from launch_options import LaunchOptions


class ServerState(Enum):
    IDLE = auto()
    LAUNCHING = auto()
    PULLING_IMAGE = auto()
    LOADING = auto()
    READY = auto()
    ERROR = auto()
    STOPPING = auto()


# vLLM trace capture context lengths — exactly 10 deterministic steps
_TRACE_LENGTHS = [128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32640, 65408]
_TRACE_INDEX = {v: i + 1 for i, v in enumerate(_TRACE_LENGTHS)}


class LogParser:
    """Parse log lines and track substage + deterministic progress signals."""

    def __init__(self):
        self.last_substage: Optional[str] = None
        self.trace_capture_count: int = 0
        self.warmup_n: Optional[int] = None
        self.warmup_total: Optional[int] = None

    def feed(self, line: str) -> Optional[ServerState]:
        """Return a new ServerState if a transition is triggered, else None."""
        l = line.strip()

        # Error (check before other patterns to catch "ERROR" keyword)
        if re.search(r'\bERROR\b|⛔|failed.*exit\s*code|exit\s*code\s*[1-9]', l, re.I):
            # Skip lines that mention error handling in code context
            if not re.search(r'error_handler|on_error|ErrorHandler', l):
                return ServerState.ERROR

        # PULLING_IMAGE
        if re.search(r'docker pull\b', l, re.I):
            return ServerState.PULLING_IMAGE

        # LOADING — vLLM engine init
        if re.search(r'Automatically detected platform.*tt\b|Starting vLLM API server|Loading checkpoint shards', l):
            if not self.last_substage:
                self.last_substage = "engine_init"
            return ServerState.LOADING

        # LOADING — media server init
        if re.search(r'Creating new Video service|Creating media server', l, re.I):
            self.last_substage = "device_init"
            return ServerState.LOADING

        # vLLM sub-stages (order matters — more specific first)
        if re.search(r'Capturing traces:.*input_seq_len=(\d+)', l):
            m = re.search(r'input_seq_len=(\d+)', l)
            if m:
                seq_len = int(m.group(1))
                self.trace_capture_count = _TRACE_INDEX.get(seq_len, self.trace_capture_count)
            self.last_substage = "trace_capture"
        elif re.search(r'Starting vLLM API server', l):
            self.last_substage = "api_startup"
        elif re.search(r'Allocating kv cache', l, re.I):
            self.last_substage = "kv_cache"
        elif re.search(r'Loading checkpoint shards|Loading model weights', l):
            self.last_substage = "loading_weights"
        elif re.search(r'multidevice with \d+ devices.*created', l, re.I):
            self.last_substage = "device_setup"

        # Media server sub-stages
        if re.search(r'Created mesh device with \d+ devices', l):
            self.last_substage = "mesh_created"
        elif re.search(r'Device.*: Loading model', l):
            self.last_substage = "loading_weights"
        elif re.search(r'loading cache at.*(transformer|text_encoder|vae)', l, re.I):
            self.last_substage = "cache_loading"
        elif re.search(r'Model loaded successfully', l, re.I):
            self.last_substage = "model_loaded"
        elif re.search(r'All devices are warmed up and ready', l, re.I):
            self.last_substage = "warmup_complete"

        # WAN 2.2 warmup progress: "50%|█████ | 1/2 [00:41<00:41]"
        m = re.search(r'(\d+)%\|.*?\|\s*(\d+)/(\d+)\s*\[', l)
        if m:
            self.warmup_n = int(m.group(2))
            self.warmup_total = int(m.group(3))
            self.last_substage = "warmup"

        return None


@dataclass
class LaunchConfig:
    repo_path: Path
    model_name: str        # HF repo path e.g. "meta-llama/Llama-3.2-1B"
    device: str            # e.g. "P300X2"
    port: str = "8000"
    hf_token: Optional[str] = None
    no_auth: bool = True
    docker_image_override: str = ""   # passed to --override-docker-image when set
    # Optional LaunchOptions (carries advanced flags from the config panel).
    # When present, options.docker_image_override takes precedence over the
    # top-level docker_image_override field (but both are checked so that
    # callers which populate only the flat field still work correctly).
    options: Optional["LaunchOptions"] = None
    # Model inference engine (e.g. "vllm", "media", "forge"); forwarded to
    # build_extra_args so it can decide which engine-specific flags to emit.
    inference_engine: str = ""


class ServerManager:
    # Docker image-not-found patterns — capture the full image reference
    _IMAGE_NOT_FOUND_RE = re.compile(
        r'failed to resolve reference\s+"([^"]+)"'
        r'|Error response from daemon[^"]*"([^"]+)".*not found'
        r'|manifest unknown[^"]*"([^"]+)"',
        re.I,
    )

    # ModuleNotFoundError / ImportError — capture the top-level module name
    _MISSING_MODULE_RE = re.compile(
        r"ModuleNotFoundError: No module named '([^'.]+)",
        re.I,
    )

    # Map import names → pip package names where they differ
    _MODULE_TO_PACKAGE: dict = {
        "PIL": "Pillow",
        "cv2": "opencv-python",
        "yaml": "PyYAML",
        "sklearn": "scikit-learn",
        "bs4": "beautifulsoup4",
        "Crypto": "pycryptodome",
        "usb": "pyusb",
        "serial": "pyserial",
        "pkg_resources": "setuptools",
        "distutils": "setuptools",
        "skimage": "scikit-image",
    }

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._tail_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._log_path: Optional[Path] = None
        self._container_name: Optional[str] = None
        self._on_log_line: Optional[Callable] = None
        self.parser = LogParser()
        # auto-action state — image resolution
        self._config: Optional[LaunchConfig] = None
        self._on_state_cb: Optional[Callable] = None
        self._resolving_image: bool = False
        self._image_resolve_attempted: bool = False
        # auto-action state — missing deps
        self._installed_modules: set = set()   # modules already pip-installed this session
        self._installing_module: bool = False

    def launch(
        self,
        config: LaunchConfig,
        on_log_line: Callable[[str], None],
        on_state: Callable[[ServerState], None],
        _auto_retry: bool = False,
    ):
        """Start run.py and begin tailing its log. Callbacks are called directly."""
        self._stop_event.clear()
        self.parser = LogParser()
        self._container_name = None
        self._on_log_line = on_log_line
        self._config = config
        self._on_state_cb = on_state
        self._resolving_image = False
        self._installing_module = False
        if not _auto_retry:
            self._image_resolve_attempted = False
            self._installed_modules = set()

        cmd = [
            "python3", "run.py",
            "--model", config.model_name,
            "--workflow", "server",
            "--docker-server",
            "--service-port", config.port,
            "--tt-device", config.device.lower(),
        ]
        if config.no_auth:
            cmd.append("--no-auth")

        # docker_image_override: options.docker_image_override wins over the
        # flat config field, allowing the config panel to set a custom image
        # while preserving backward-compat with callers that use the flat field.
        _docker_img = (
            (config.options.docker_image_override if config.options else "")
            or config.docker_image_override
        )
        if _docker_img:
            cmd += ["--override-docker-image", _docker_img]

        # Append any extra run.py flags derived from LaunchOptions (use-case
        # presets, vLLM tuning knobs, dev flags, pass-through flags, etc.).
        if config.options:
            from launch_options import build_extra_args

            # _EntryProxy duck-types just enough of ModelEntry for build_extra_args:
            # it needs inference_engine (to guard vLLM-only flags) and family
            # (for auto-detecting the tool-call parser).  family is left empty
            # so the default "hermes" parser is used when tool_call_parser is
            # not explicitly set.
            class _EntryProxy:
                inference_engine = config.inference_engine or ""
                family = ""

            cmd += build_extra_args(config.options, _EntryProxy())

        env = dict(os.environ)
        if config.hf_token:
            env["HF_TOKEN"] = config.hf_token

        log.info("launch: %s  (cwd=%s)", " ".join(cmd), config.repo_path)
        on_log_line(f"$ {' '.join(cmd)}")
        on_log_line(f"  cwd: {config.repo_path}")

        self._proc = subprocess.Popen(
            cmd,
            cwd=str(config.repo_path),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Forward stderr to UI so early crashes are visible
        stderr_thread = threading.Thread(
            target=self._stderr_reader,
            args=(self._proc, on_log_line),
            daemon=True,
        )
        stderr_thread.start()

        self._tail_thread = threading.Thread(
            target=self._tail_loop,
            args=(config.repo_path, on_log_line, on_state),
            daemon=True,
        )
        self._tail_thread.start()

    def _stderr_reader(self, proc: subprocess.Popen, on_log_line: Callable):
        """Forward stderr lines to the UI and pass them through auto-action checks."""
        try:
            for line in proc.stderr:
                if self._stop_event.is_set():
                    break
                line = line.rstrip("\n")
                if not line:
                    continue
                log.debug("[stderr] %s", line)
                on_log_line(f"[stderr] {line}")
                self._check_line(line, on_log_line)
        except Exception:
            pass

    def _check_line(self, line: str, on_log_line: Callable):
        """Scan a log/stderr line for conditions that warrant automatic recovery.

        Called from both _stderr_reader and _tail_loop so we catch errors
        regardless of whether run.py emits them to its own log or to stderr.
        Guards prevent duplicate triggers.
        """
        # --- Docker image not found ---
        if not self._image_resolve_attempted:
            m = self._IMAGE_NOT_FOUND_RE.search(line)
            if m:
                failed_ref = next(g for g in m.groups() if g)
                self._image_resolve_attempted = True
                self._resolving_image = True
                log.warning("image not found: %s — starting GHCR resolution", failed_ref)
                on_log_line(f"⚠ Image not found: {failed_ref} — querying GHCR for latest tag…")
                threading.Thread(
                    target=self._try_resolve_image,
                    args=(failed_ref, on_log_line),
                    daemon=True,
                ).start()

        # --- Missing Python module ---
        if not self._installing_module:
            m = self._MISSING_MODULE_RE.search(line)
            if m:
                module = m.group(1)
                if module not in self._installed_modules:
                    package = self._MODULE_TO_PACKAGE.get(module, module)
                    self._installing_module = True
                    self._installed_modules.add(module)
                    on_log_line(f"⚠ Missing module '{module}' — installing {package}…")
                    threading.Thread(
                        target=self._try_install_module,
                        args=(module, package, on_log_line),
                        daemon=True,
                    ).start()

    def _try_resolve_image(self, failed_ref: str, on_log_line: Callable):
        """Background: resolve a better GHCR tag and restart the launch."""
        from ghcr_resolver import resolve_latest_tag

        resolved = resolve_latest_tag(failed_ref, on_step=on_log_line)
        if not resolved:
            log.error("GHCR resolution failed for %s", failed_ref)
            on_log_line("✗ Could not resolve a newer tag from GHCR — check image manually")
            self._resolving_image = False
            if self._on_state_cb:
                self._on_state_cb(ServerState.ERROR)
            return

        log.info("GHCR resolved %s → %s", failed_ref, resolved)
        on_log_line(f"✓ Resolved → {resolved}")
        on_log_line("↺ Restarting launch with resolved image…")

        # Wait briefly for current process to fully exit
        if self._proc:
            for _ in range(20):
                if self._proc.poll() is not None:
                    break
                time.sleep(0.25)

        # Stop old threads, then relaunch with the resolved image
        self._stop_event.set()
        time.sleep(0.3)

        import dataclasses
        new_config = dataclasses.replace(self._config, docker_image_override=resolved)
        # _auto_retry=True keeps _image_resolve_attempted=True to prevent a second loop
        self.launch(new_config, on_log_line, self._on_state_cb, _auto_retry=True)

    def _try_install_module(self, module: str, package: str, on_log_line: Callable):
        """Background: pip-install a missing module then restart the launch."""
        repo_path = self._config.repo_path if self._config else Path.cwd()
        try:
            result = subprocess.run(
                ["python3", "-m", "pip", "install", "--quiet", package],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                on_log_line(f"✓ Installed {package}")
            else:
                err = (result.stderr or result.stdout).strip().splitlines()[-1] if (result.stderr or result.stdout) else "unknown error"
                on_log_line(f"✗ pip install {package} failed: {err}")
                self._installing_module = False
                return
        except Exception as exc:
            on_log_line(f"✗ pip install {package} error: {exc}")
            self._installing_module = False
            return

        on_log_line("↺ Restarting launch after module install…")

        if self._proc:
            for _ in range(20):
                if self._proc.poll() is not None:
                    break
                time.sleep(0.25)

        self._stop_event.set()
        time.sleep(0.3)
        self._installing_module = False

        if self._config and self._on_state_cb:
            self.launch(self._config, on_log_line, self._on_state_cb, _auto_retry=True)

    def _find_log_file(self, repo_path: Path, timeout: float = 20.0) -> Optional[Path]:
        # Candidate log directories in preference order
        log_dirs = [
            repo_path / "workflow_logs" / "run_logs",
            repo_path / "logs",
            repo_path / "run_logs",
        ]
        before: dict = {}
        for ld in log_dirs:
            before[ld] = set(ld.glob("*.log")) if ld.exists() else set()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                return None
            # Check if process exited early
            if self._proc and self._proc.poll() is not None:
                rc = self._proc.returncode
                if not self._resolving_image:
                    self._on_log_line(f"⚠ run.py exited with code {rc} before creating log file")
                return None
            for ld in log_dirs:
                if ld.exists():
                    current = set(ld.glob("*.log"))
                    new = current - before[ld]
                    if new:
                        return max(new, key=lambda p: p.stat().st_mtime)
            time.sleep(0.5)
        return None

    def _tail_loop(
        self,
        repo_path: Path,
        on_log_line: Callable,
        on_state: Callable,
    ):
        log_path = self._find_log_file(repo_path)
        if log_path is None:
            if not self._resolving_image:
                on_log_line("⚠ Log file not found — subprocess may have failed")
                on_state(ServerState.ERROR)
            return

        self._log_path = log_path

        with open(log_path, "r", errors="replace") as f:
            while not self._stop_event.is_set():
                line = f.readline()
                if not line:
                    if self._proc and self._proc.poll() is not None:
                        if self._proc.returncode != 0:
                            on_state(ServerState.ERROR)
                        break
                    time.sleep(0.05)
                    continue

                line = line.rstrip("\n")
                on_log_line(line)
                self._check_line(line, on_log_line)

                new_state = self.parser.feed(line)
                if new_state:
                    log.debug("state→%s  (line: %s)", new_state.name, line[:120])
                    on_state(new_state)

                # Detect container name from "docker run ... --name <name>"
                m = re.search(r'--name\s+(\S+)', line)
                if m and not self._container_name:
                    self._container_name = m.group(1)
                    log.info("container name: %s", self._container_name)

    def stop(self):
        """Stop the server. Non-blocking."""
        self._stop_event.set()
        if self._container_name:
            subprocess.Popen(
                ["docker", "stop", self._container_name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        elif self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def get_container_name(self) -> Optional[str]:
        return self._container_name
