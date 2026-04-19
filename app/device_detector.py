#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Detect TT hardware via tt-smi -s and map to model_spec device type strings."""
import json
import re
import subprocess
from typing import List, Optional

# Map board_type (from tt-smi) to model_spec DeviceType names
_BOARD_TO_DEVICE = {
    "n150":     "N150",
    "n300":     "N300",
    "p100":     "P100",
    "p150":     "P150",
    "p300c":    "P300X2",  # QB2 = 4× p300c dies, each pair is one P300X2 card
    "p300":     "P300",
    "t3000":    "T3K",
    "galaxy":   "P150X8",
    "blackhole": "BLACKHOLE_GALAXY",
}

# When a multi-die board is detected, also expose single-die subsets
_SUPERSET_INCLUDES = {
    "P300X2": ["P300", "P150X4"],
    "T3K":    ["P150", "N150", "P150X4"],
    "N300":   ["N150"],
}


def board_type_to_device(board_type: str) -> Optional[str]:
    return _BOARD_TO_DEVICE.get(board_type.lower())


def detect_devices_from_json(json_str: str) -> List[str]:
    """Parse tt-smi -s output and return unique DeviceType strings.

    Handles two schema variants:
      - Flat: device_info[i].board_type  (older snapshots / tests)
      - Nested: device_info[i].board_info.board_type  (tt-smi 5.x)
    Falls back to regex extraction when JSON is malformed (e.g. two concatenated objects).
    """
    data = None
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        pass

    if data is None:
        # Fallback: pull board_type values via regex
        board_types = re.findall(r'"board_type"\s*:\s*"([^"]+)"', json_str)
        if not board_types:
            return []
        data = {"device_info": [{"board_type": bt} for bt in board_types]}

    seen = set()
    result = []
    for entry in data.get("device_info", []):
        # Try nested board_info first (tt-smi 5.x), then flat (older / tests)
        board_info = entry.get("board_info") if isinstance(entry.get("board_info"), dict) else {}
        bt = board_info.get("board_type") or entry.get("board_type", "")
        device = board_type_to_device(bt)
        if device and device not in seen:
            seen.add(device)
            result.append(device)
            for extra in _SUPERSET_INCLUDES.get(device, []):
                if extra not in seen:
                    seen.add(extra)
                    result.append(extra)
    return result


def detect_devices() -> List[str]:
    """Run tt-smi -s and return detected DeviceType strings. Returns [] on failure."""
    try:
        out = subprocess.run(
            ["tt-smi", "-s"], capture_output=True, text=True, timeout=10
        )
        return detect_devices_from_json(out.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
