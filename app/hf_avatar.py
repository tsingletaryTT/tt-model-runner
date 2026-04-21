#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Async HuggingFace organisation / user avatar fetcher with disk cache.

Usage:
    fetch_avatar_async("meta-llama", on_done=lambda data: ...)

on_done is called on the fetch thread with raw PNG/JPEG bytes, or None on
failure.  Callers must dispatch to the GTK main loop before touching widgets:
    GLib.idle_add(lambda: image.set_from_pixbuf(make_pixbuf(data)))
"""
import json
import logging
import threading
import urllib.request
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

_CACHE_DIR = Path.home() / ".config" / "tt-runner-gui" / "avatars"
_TIMEOUT = 8
_UA = "tt-model-runner/1.0 (https://github.com/tenstorrent/tt-model-runner)"


def _cache_path(org: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # Sanitise org name so it's safe as a filename
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in org)
    return _CACHE_DIR / f"{safe}.img"


def _get(url: str) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.read()
    except Exception as exc:
        log.debug("hf_avatar GET %s: %s", url, exc)
        return None


def _fetch_avatar_url(org: str) -> Optional[str]:
    """Try HF organisation endpoint, then user endpoint; return avatarUrl or None."""
    for kind in ("organizations", "users"):
        raw = _get(f"https://huggingface.co/api/{kind}/{org}")
        if not raw:
            continue
        try:
            data = json.loads(raw)
            url = data.get("avatarUrl") or data.get("avatar_url")
            if url:
                return url
        except (json.JSONDecodeError, AttributeError):
            continue
    return None


def fetch_avatar_async(
    org: str,
    on_done: Callable[[Optional[bytes]], None],
) -> None:
    """Fetch avatar image bytes for *org* in a daemon thread.

    Hits disk cache first; only makes network requests on cache miss.
    Calls on_done(bytes) on success, on_done(None) on failure.
    on_done is called on the background thread — dispatch to GTK loop before
    touching any widgets.
    """
    def _run() -> None:
        cached = _cache_path(org)
        if cached.exists():
            try:
                on_done(cached.read_bytes())
                return
            except OSError:
                pass

        avatar_url = _fetch_avatar_url(org)
        if not avatar_url:
            log.debug("hf_avatar: no avatar URL for %s", org)
            on_done(None)
            return

        data = _get(avatar_url)
        if data:
            try:
                cached.write_bytes(data)
            except OSError:
                pass
            on_done(data)
        else:
            on_done(None)

    threading.Thread(target=_run, daemon=True, name=f"hf-avatar-{org}").start()
