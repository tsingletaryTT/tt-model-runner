#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Background thread polling /v1/models (vLLM) or /tt-liveness (media server).

Calls on_ready once on first HTTP 200. Calls on_lost if health fails after READY.
All callbacks go through GLib.idle_add — never touches widgets directly.
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
        poll_interval: float = 5.0,
        engine: str = "vllm",
    ):
        super().__init__(daemon=True, name="HealthWorker")
        self._port = port
        self._on_ready = on_ready
        self._on_lost = on_lost
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
                r = _requests.get(f"http://localhost:{self._port}/tt-liveness", timeout=3)
                if r.status_code == 200:
                    return [r.json().get("model", "unknown")]
            else:
                r = _requests.get(f"http://localhost:{self._port}/v1/models", timeout=3)
                if r.status_code == 200:
                    data = r.json()
                    return [m.get("id", "unknown") for m in data.get("data", [])]
        except Exception:
            pass
        return None

    def run(self):
        from worker import idle_add_once
        while not self._stop.is_set():
            models = self._check()
            if models is not None and not self._was_ready:
                self._was_ready = True
                idle_add_once(self._on_ready, models)
            elif models is None and self._was_ready:
                self._was_ready = False
                idle_add_once(self._on_lost)
            self._stop.wait(self._poll_interval)
