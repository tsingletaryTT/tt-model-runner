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
        base = f"http://localhost:{self._port}"
        endpoints = []
        if self._engine == "media":
            endpoints = [("media", f"{base}/tt-liveness")]
        elif self._engine == "auto":
            # Try vLLM first (more common), then media server endpoint
            endpoints = [("vllm", f"{base}/v1/models"), ("media", f"{base}/tt-liveness")]
        else:
            endpoints = [("vllm", f"{base}/v1/models")]

        for kind, url in endpoints:
            try:
                r = _requests.get(url, timeout=3)
                if r.status_code == 200:
                    if kind == "media":
                        return [r.json().get("model", "unknown")]
                    else:
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
