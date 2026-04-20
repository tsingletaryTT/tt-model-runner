# app/compat_catalog.py
# SPDX-License-Identifier: Apache-2.0
"""Fetch and cache the Tenstorrent model compatibility catalog.

Covers 222 models across tt-inference-server, tt-forge, and tt-metal.
Cached for 24 h at ~/.cache/tt-runner-gui/compatibility.json.
"""
import json
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

_URL = "https://d1oi7xemha0dsy.cloudfront.net/data/compatibility.json"
_CACHE_PATH = Path.home() / ".cache" / "tt-runner-gui" / "compatibility.json"
_CACHE_TTL = 86400  # 24 hours

# Maps compatibility.json hardware names (lowercase) to internal device_type IDs.
_HW_MAP: Dict[str, str] = {
    "n150": "N150",
    "n300": "N300",
    "p100": "P100",
    "p150": "P150",
    "p300": "P300",
    "galaxy": "T3K",             # Galaxy = T3K (8× WH ring)
    "quietbox": "P150X4",        # Quietbox (gen 1) = P150X4 (4× N150)
    "quietbox 2": "P300X2",      # Quietbox 2 = P300X2 (2× P300 Blackhole)
    "2 x quietbox": "P150X8",    # 2× Quietbox = P150X8 (8× N150)
    "loudbox": "P300X2",         # Loudbox = P300X2
    "2 x galaxy": "P150X8",
}


@dataclass
class HardwareCompat:
    hardware: str            # original hardware name from JSON
    chip_set: str
    hardware_family: str
    status: str              # "Supported" | "Experimental" | "Not Supported"
    software: List[str]      # ["tt-forge", "tt-inference-server", "tt-metal"]


@dataclass
class CompatEntry:
    id: str
    display_name: str
    family: str
    tasks: List[str]
    compatibility: List[HardwareCompat]
    model_description: str = ""
    model_size: str = ""
    model_size_num: Optional[float] = None


class CompatCatalog:
    """Parsed representation of compatibility.json.  Thread-safe read after init."""

    def __init__(self, entries: List[CompatEntry]):
        self._entries = entries
        self._by_id: Dict[str, CompatEntry] = {e.id.lower(): e for e in entries}

    @classmethod
    def _fetch_raw(cls) -> dict:
        """Return parsed JSON dict, trying cache then network."""
        if _CACHE_PATH.exists():
            if time.time() - _CACHE_PATH.stat().st_mtime < _CACHE_TTL:
                try:
                    return json.loads(_CACHE_PATH.read_text())
                except (json.JSONDecodeError, OSError):
                    pass
        with urllib.request.urlopen(_URL, timeout=10) as resp:
            raw = resp.read()
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_bytes(raw)
        return json.loads(raw)

    @classmethod
    def load(cls) -> "CompatCatalog":
        data = cls._fetch_raw()
        entries = [
            CompatEntry(
                id=m.get("id", ""),
                display_name=m.get("display_name", ""),
                family=m.get("family", ""),
                tasks=m.get("tasks", []),
                compatibility=[
                    HardwareCompat(
                        hardware=c.get("hardware", ""),
                        chip_set=c.get("chip_set", ""),
                        hardware_family=c.get("hardware_family", ""),
                        status=c.get("status", "Not Supported"),
                        software=c.get("software", []),
                    )
                    for c in m.get("compatibility", [])
                ],
                model_description=m.get("model_description", ""),
                model_size=m.get("model_size", ""),
                model_size_num=m.get("model_size_num"),
            )
            for m in data.get("models", [])
        ]
        return cls(entries)

    def lookup(self, model_id: str) -> Optional[CompatEntry]:
        """Look up entry by id (case-insensitive)."""
        return self._by_id.get(model_id.lower())

    def lookup_by_display_name(self, display_name: str) -> Optional[CompatEntry]:
        """Look up entry by display_name (case-insensitive)."""
        dn = display_name.lower()
        for e in self._entries:
            if e.display_name.lower() == dn:
                return e
        return None

    def get_for_hardware(self, device_type: str, *,
                         software: Optional[str] = None,
                         include_experimental: bool = True) -> List[CompatEntry]:
        """Models supported on device_type (internal ID e.g. N150, T3K).

        software filters to a specific stack: "tt-inference-server", "tt-forge", "tt-metal".
        """
        matching_hw = {hw for hw, dt in _HW_MAP.items() if dt == device_type}
        matching_hw.add(device_type.lower())
        results = []
        for e in self._entries:
            for c in e.compatibility:
                if c.hardware.lower() not in matching_hw:
                    continue
                if c.status == "Not Supported":
                    continue
                if c.status == "Experimental" and not include_experimental:
                    continue
                if software and software not in c.software:
                    continue
                results.append(e)
                break
        return results

    def all_entries(self) -> List[CompatEntry]:
        return list(self._entries)


def load_async(on_done: Callable[[Optional[CompatCatalog]], None]) -> None:
    """Fetch and parse the catalog in a background thread.

    on_done(catalog) on success or on_done(None) on failure.
    Called from background thread — caller must dispatch to UI event loop if needed.
    """
    def _run():
        try:
            on_done(CompatCatalog.load())
        except Exception:
            on_done(None)

    threading.Thread(target=_run, daemon=True).start()
