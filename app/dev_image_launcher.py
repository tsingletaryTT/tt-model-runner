# app/dev_image_launcher.py
# SPDX-License-Identifier: Apache-2.0
"""Launch a model via the tt-developer-image Docker container.

Convention: model scripts live at
  {dev_image_repo}/models/{model_id}/{software_stack}.py
e.g. ~/code/tt-developer-image/models/llama-3-3-70b/tt-forge.py

The container mounts the dev image repo at /workspace so scripts can import
shared helpers from there.

No HTTP health endpoint — readiness is inferred from container-alive status
and log output.  Container-alive checked every 5 s via docker inspect.
"""
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from server_manager import ServerState

# Maps software stack names to the env-activation script inside the container.
_SW_ENV: dict = {
    "tt-forge": "/etc/profile.d/tt-env-forge.sh",
    "tt-metal": "/etc/profile.d/tt-env-metal.sh",
    "tt-vllm":  "/etc/profile.d/tt-env-vllm.sh",
}

_DEFAULT_IMAGE = "tenstorrent/dev-n150:latest"


@dataclass
class DevLaunchConfig:
    dev_image_repo: Path    # path to tt-developer-image checkout
    model_id: str           # compatibility.json model id (e.g. "bge-large-en-v1-5")
    software_stack: str     # "tt-forge" | "tt-metal" | "tt-vllm"
    docker_image: str = _DEFAULT_IMAGE
    device_path: str = "/dev/tenstorrent"


class DevImageLauncher:
    """Runs a model script inside the tt-developer-image Docker container.

    Public API mirrors the relevant parts of ServerManager:
      launch(config, on_log_line, on_state)
      stop()
      has_script(config) -> bool
    """

    def __init__(self):
        self._container_name: Optional[str] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── Public API ────────────────────────────────────────────────────────────

    @staticmethod
    def has_script(config: DevLaunchConfig) -> bool:
        """Return True if the model script exists in the dev image repo."""
        script = (config.dev_image_repo / "models"
                  / config.model_id / f"{config.software_stack}.py")
        return script.exists()

    @staticmethod
    def get_available_stacks(dev_image_repo: Path, model_id: str) -> list:
        """Return list of software stacks that have scripts for model_id.

        Returns e.g. ["tt-forge", "tt-metal"] based on which .py files exist.
        Empty list when dev_image_repo doesn't exist or has no models/ dir.
        """
        model_dir = dev_image_repo / "models" / model_id
        if not model_dir.exists():
            return []
        stacks = []
        for sw in ("tt-forge", "tt-metal", "tt-vllm"):
            if (model_dir / f"{sw}.py").exists():
                stacks.append(sw)
        return stacks

    @staticmethod
    def scan_all_models(dev_image_repo: Path) -> dict:
        """Return {model_id: [stacks]} for every model in dev_image_repo/models/.

        Used to surface the inventory in the UI without querying per-model.
        Returns empty dict when repo doesn't exist.
        """
        models_dir = dev_image_repo / "models"
        if not models_dir.exists():
            return {}
        result = {}
        for model_dir in sorted(models_dir.iterdir()):
            if not model_dir.is_dir():
                continue
            stacks = []
            for sw in ("tt-forge", "tt-metal", "tt-vllm"):
                if (model_dir / f"{sw}.py").exists():
                    stacks.append(sw)
            if stacks:
                result[model_dir.name] = stacks
        return result

    def launch(self, config: DevLaunchConfig,
               on_log_line: Callable[[str], None],
               on_state: Callable[[ServerState], None]) -> None:
        self._stop_event.clear()
        self._container_name = None
        self._thread = threading.Thread(
            target=self._run, args=(config, on_log_line, on_state), daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        name = self._container_name
        if name:
            try:
                subprocess.run(
                    ["docker", "stop", "-t", "5", name],
                    check=False, capture_output=True,
                )
            except Exception:
                pass

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run(self, config: DevLaunchConfig,
             on_log_line: Callable[[str], None],
             on_state: Callable[[ServerState], None]) -> None:
        script_rel = f"/workspace/models/{config.model_id}/{config.software_stack}.py"
        env_script = _SW_ENV.get(config.software_stack, _SW_ENV["tt-forge"])
        cmd_str = f"source {env_script} && python {script_rel}"
        cname = f"tt-dev-{config.model_id.replace('/', '-')[:40]}-{int(time.time())}"
        self._container_name = cname

        docker_cmd = [
            "docker", "run",
            "--name", cname,
            "--rm",
            "--device", config.device_path,
            "-v", f"{config.dev_image_repo}:/workspace",
            config.docker_image,
            "bash", "-c", cmd_str,
        ]

        on_log_line(f"▶ docker run {config.docker_image}  [{config.software_stack}]")
        on_log_line(f"  Script: {script_rel}")
        on_state(ServerState.LAUNCHING)

        try:
            proc = subprocess.Popen(
                docker_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except FileNotFoundError:
            on_log_line("✗ docker not found — is Docker installed?")
            on_state(ServerState.ERROR)
            return

        on_state(ServerState.RUNNING)

        last_check = time.monotonic()
        for line in proc.stdout:
            if self._stop_event.is_set():
                break
            on_log_line(line.rstrip("\n"))
            now = time.monotonic()
            if now - last_check >= 5.0:
                last_check = now
                if not self._is_container_running(cname):
                    break

        rc = proc.wait()
        if self._stop_event.is_set():
            on_state(ServerState.IDLE)
        elif rc == 0:
            on_log_line("✓ Script completed successfully")
            on_state(ServerState.DONE)
        else:
            on_log_line(f"✗ Script exited with code {rc}")
            on_state(ServerState.ERROR)

    @staticmethod
    def _is_container_running(name: str) -> bool:
        try:
            out = subprocess.check_output(
                ["docker", "inspect", "--format={{.State.Running}}", name],
                stderr=subprocess.DEVNULL, text=True,
            ).strip()
            return out == "true"
        except Exception:
            return False
