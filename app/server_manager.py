#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Launch run.py, tail its log file, stop container.

All on_log_line / on_state callbacks are posted via GLib.idle_add — never
called directly from the tail thread.
"""
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Callable, List, Optional


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


class ServerManager:
    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._tail_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._log_path: Optional[Path] = None
        self._container_name: Optional[str] = None
        self._on_log_line: Optional[Callable] = None
        self.parser = LogParser()
        # image auto-resolution state
        self._config: Optional[LaunchConfig] = None
        self._on_state_cb: Optional[Callable] = None
        self._resolving_image: bool = False
        self._image_resolve_attempted: bool = False

    def launch(
        self,
        config: LaunchConfig,
        on_log_line: Callable[[str], None],
        on_state: Callable[[ServerState], None],
        _auto_retry: bool = False,
    ):
        """Start run.py and begin tailing its log. All callbacks via GLib.idle_add."""
        self._stop_event.clear()
        self.parser = LogParser()
        self._container_name = None
        self._on_log_line = on_log_line
        self._config = config
        self._on_state_cb = on_state
        self._resolving_image = False
        if not _auto_retry:
            self._image_resolve_attempted = False

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
        if config.docker_image_override:
            cmd += ["--override-docker-image", config.docker_image_override]

        env = dict(os.environ)
        if config.hf_token:
            env["HF_TOKEN"] = config.hf_token

        from worker import idle_add_once
        idle_add_once(on_log_line, f"$ {' '.join(cmd)}")
        idle_add_once(on_log_line, f"  cwd: {config.repo_path}")

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

    # Pattern: Docker "not found" / "failed to resolve reference" with image ref
    _IMAGE_NOT_FOUND_RE = re.compile(
        r'failed to resolve reference\s+"([^"]+)".*not found'
        r'|manifest unknown.*"([^"]+)"'
        r'|Error response from daemon.*"([^"]+)".*not found',
        re.I,
    )

    def _stderr_reader(self, proc: subprocess.Popen, on_log_line: Callable):
        """Forward stderr lines to the UI until the process ends.

        Also watches for Docker image-not-found errors and triggers auto-resolution.
        """
        from worker import idle_add_once
        try:
            for line in proc.stderr:
                if self._stop_event.is_set():
                    break
                line = line.rstrip("\n")
                if not line:
                    continue
                idle_add_once(on_log_line, f"[stderr] {line}")

                # Check for image-not-found — only attempt once per launch
                if not self._image_resolve_attempted:
                    m = self._IMAGE_NOT_FOUND_RE.search(line)
                    if m:
                        failed_ref = next(g for g in m.groups() if g)
                        self._image_resolve_attempted = True
                        self._resolving_image = True
                        idle_add_once(
                            on_log_line,
                            f"⚠ Image not found: {failed_ref} — querying GHCR for latest tag…",
                        )
                        t = threading.Thread(
                            target=self._try_resolve_image,
                            args=(failed_ref, on_log_line),
                            daemon=True,
                        )
                        t.start()
        except Exception:
            pass

    def _try_resolve_image(self, failed_ref: str, on_log_line: Callable):
        """Background: resolve a better GHCR tag and restart the launch."""
        from worker import idle_add_once
        from ghcr_resolver import resolve_latest_tag

        resolved = resolve_latest_tag(failed_ref)
        if not resolved:
            idle_add_once(on_log_line, "✗ Could not resolve a newer tag from GHCR — check image manually")
            self._resolving_image = False
            if self._on_state_cb:
                idle_add_once(self._on_state_cb, ServerState.ERROR)
            return

        idle_add_once(on_log_line, f"✓ Resolved → {resolved}")
        idle_add_once(on_log_line, "↺ Restarting launch with resolved image…")

        # Wait briefly for current process to fully exit
        if self._proc:
            for _ in range(20):
                if self._proc.poll() is not None:
                    break
                time.sleep(0.25)

        # Stop old threads, then relaunch with the resolved image
        self._stop_event.set()
        time.sleep(0.3)

        new_config = LaunchConfig(
            repo_path=self._config.repo_path,
            model_name=self._config.model_name,
            device=self._config.device,
            port=self._config.port,
            hf_token=self._config.hf_token,
            no_auth=self._config.no_auth,
            docker_image_override=resolved,
        )
        # _auto_retry=True keeps _image_resolve_attempted=True to prevent a second loop
        self.launch(new_config, on_log_line, self._on_state_cb, _auto_retry=True)

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
                    from worker import idle_add_once
                    idle_add_once(
                        self._on_log_line,
                        f"⚠ run.py exited with code {rc} before creating log file",
                    )
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
        from worker import idle_add_once

        log_path = self._find_log_file(repo_path)
        if log_path is None:
            if not self._resolving_image:
                idle_add_once(on_log_line, "⚠ Log file not found — subprocess may have failed")
                idle_add_once(on_state, ServerState.ERROR)
            return

        self._log_path = log_path

        with open(log_path, "r", errors="replace") as f:
            while not self._stop_event.is_set():
                line = f.readline()
                if not line:
                    if self._proc and self._proc.poll() is not None:
                        if self._proc.returncode != 0:
                            idle_add_once(on_state, ServerState.ERROR)
                        break
                    time.sleep(0.05)
                    continue

                line = line.rstrip("\n")
                idle_add_once(on_log_line, line)

                new_state = self.parser.feed(line)
                if new_state:
                    idle_add_once(on_state, new_state)

                # Detect container name from "docker run ... --name <name>"
                m = re.search(r'--name\s+(\S+)', line)
                if m and not self._container_name:
                    self._container_name = m.group(1)

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
