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
from dataclasses import dataclass, field
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


class ServerManager:
    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._tail_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._log_path: Optional[Path] = None
        self._container_name: Optional[str] = None
        self.parser = LogParser()

    def launch(
        self,
        config: LaunchConfig,
        on_log_line: Callable[[str], None],
        on_state: Callable[[ServerState], None],
    ):
        """Start run.py and begin tailing its log. All callbacks via GLib.idle_add."""
        self._stop_event.clear()
        self.parser = LogParser()
        self._container_name = None

        cmd = [
            "python3", "run.py",
            "--model", config.model_name,
            "--workflow", "server",
            "--docker-server",
            "--service-port", config.port,
        ]
        if config.no_auth:
            cmd.append("--no-auth")

        env = dict(os.environ)
        if config.hf_token:
            env["HF_TOKEN"] = config.hf_token

        self._proc = subprocess.Popen(
            cmd,
            cwd=str(config.repo_path),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        self._tail_thread = threading.Thread(
            target=self._tail_loop,
            args=(config.repo_path, on_log_line, on_state),
            daemon=True,
        )
        self._tail_thread.start()

    def _find_log_file(self, repo_path: Path, timeout: float = 15.0) -> Optional[Path]:
        log_dir = repo_path / "workflow_logs" / "run_logs"
        deadline = time.monotonic() + timeout
        before = set(log_dir.glob("run_*.log")) if log_dir.exists() else set()
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                return None
            if log_dir.exists():
                current = set(log_dir.glob("run_*.log"))
                new = current - before
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
            idle_add_once(on_log_line, "⚠ Log file not found after 15s — subprocess may have failed")
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
