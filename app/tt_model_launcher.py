#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Launch a tt-model-manager community bundle via `tt serve`.

Shells the `tt` binary (not `tt-model` directly): `tt-model` is a
lazily-installed tool `tt` itself manages (installed on first use, pinned to
a golden version, with its own HF_HOME env pinning) — shelling it directly
would bypass all of that and fail with "not found" on a machine that has
never served a community bundle before. `tt serve <bundle_id>` and
`tt model stop <bundle_id>` both dispatch correctly by bundle-id shape on
tt-cli's side; safe here because this launcher is only ever invoked for
entries already tagged source == "community".

Unlike ServerManager (which drives run.py inside Docker, with GHCR image
resolution and auto-remediation) this is a single self-contained foreground
process — no container to poll, no image to resolve. Reuses ServerManager's
LogParser since vLLM-backed bundles produce the same startup log patterns
run.py's own images do; readiness is left to the existing HealthWorker
(polls the configured port), not detected here.
"""
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from server_manager import LogParser, ServerState


@dataclass
class TtModelLaunchConfig:
    bundle_id: str          # e.g. "episod/tt-animatediff"
    port: str = "8000"
    profile: str = ""       # --profile NAME; empty omits the flag


class TtModelLauncher:
    """Runs `tt serve <bundle_id>` and tails its output.

    Public API mirrors ServerManager/DevImageLauncher:
        launch(config, on_log_line, on_state)
        stop()
    """

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._bundle_id: Optional[str] = None
        self.parser = LogParser()

    def launch(self, config: TtModelLaunchConfig,
               on_log_line: Callable[[str], None],
               on_state: Callable[[ServerState], None]) -> None:
        self._stop_event.clear()
        self.parser = LogParser()
        self._bundle_id = config.bundle_id
        self._thread = threading.Thread(
            target=self._run, args=(config, on_log_line, on_state), daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        bundle_id = self._bundle_id
        if bundle_id:
            tt_bin = shutil.which("tt")
            if tt_bin:
                try:
                    subprocess.run(
                        [tt_bin, "model", "stop", bundle_id],
                        check=False, capture_output=True, timeout=15,
                    )
                except Exception:
                    pass
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def _run(self, config: TtModelLaunchConfig,
              on_log_line: Callable[[str], None],
              on_state: Callable[[ServerState], None]) -> None:
        tt_bin = shutil.which("tt")
        if not tt_bin:
            on_log_line("✗ tt not found — install the tt CLI toolchain")
            on_state(ServerState.ERROR)
            return

        cmd = [tt_bin, "serve", config.bundle_id, "--port", str(config.port)]
        if config.profile:
            cmd += ["--profile", config.profile]

        on_log_line(f"▶ {' '.join(cmd)}")
        on_state(ServerState.LAUNCHING)

        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
        except FileNotFoundError:
            on_log_line("✗ tt not found — install the tt CLI toolchain")
            on_state(ServerState.ERROR)
            return

        for line in self._proc.stdout:
            if self._stop_event.is_set():
                break
            stripped = line.rstrip("\n")
            on_log_line(stripped)
            new_state = self.parser.feed(stripped)
            if new_state is not None:
                on_state(new_state)

        rc = self._proc.wait()
        if self._stop_event.is_set():
            on_state(ServerState.IDLE)
        elif rc != 0:
            on_log_line(f"✗ tt serve exited with code {rc}")
            on_state(ServerState.ERROR)
