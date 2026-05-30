#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Detect TT hardware via tt-smi -s and map to model_spec device type strings."""
import json
import re
import subprocess
from dataclasses import dataclass
from typing import List, Optional

# Map board_type (from tt-smi) to model_spec DeviceType names.
# "galaxy" → GALAXY is the Blackhole Galaxy board (replaces stale BLACKHOLE_GALAXY entry).
_BOARD_TO_DEVICE = {
    "n150":   "N150",
    "n300":   "N300",
    "p100":   "P100",
    "p150":   "P150",
    "p300c":  "P300X2",  # QB2 = 4× p300c dies, each pair is one P300X2 card
    "p300":   "P300",
    "t3000":  "T3K",
    "galaxy": "GALAXY",  # Blackhole Galaxy board
}

# When a multi-die or compound topology is detected, also expose the subset
# topologies that fit inside it (so model_spec entries requiring a smaller
# topology are still offered).
_SUPERSET_INCLUDES = {
    # Blackhole multi-chip boards
    "P300X2":      ["P300", "P150", "P100", "P150X4"],  # QB2 4×p300c: all BH-family subsets fit
    # Wormhole multi-chip boards
    "T3K":         ["P150", "N150", "P150X4"],
    "N300":        ["N150"],
    "N150X4":      ["N150"],
    # Galaxy / multi-Galaxy compound topologies
    "GALAXY":      ["P150X8"],
    "DUAL_GALAXY": ["GALAXY", "P150X8"],
    "QUAD_GALAXY":  ["DUAL_GALAXY", "GALAXY", "P150X8"],
    "GALAXY_T3K":  ["GALAXY", "T3K", "P150X8"],
}


@dataclass
class ChipStatus:
    """Per-chip telemetry snapshot from tt-smi -s."""
    index: int
    board_type: str
    temp_c: Optional[float]        # ASIC temperature in °C
    aiclk_mhz: Optional[int]       # AI clock in MHz
    fw_version: str


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

    # --- Pass 1: unique-type pass ---
    # Emit one entry per distinct board_type and expand its superset subsets.
    # Also accumulate raw board_type counts for the count-based pass below.
    raw_counts: dict = {}  # board_type (lowercase) → count of chip entries
    for entry in data.get("device_info", []):
        # Try nested board_info first (tt-smi 5.x), then flat (older / tests)
        board_info = entry.get("board_info") if isinstance(entry.get("board_info"), dict) else {}
        bt = (board_info.get("board_type") or entry.get("board_type", "")).lower()
        if bt:
            raw_counts[bt] = raw_counts.get(bt, 0) + 1
        device = board_type_to_device(bt)
        if device and device not in seen:
            seen.add(device)
            result.append(device)
            for extra in _SUPERSET_INCLUDES.get(device, []):
                if extra not in seen:
                    seen.add(extra)
                    result.append(extra)

    # --- Pass 2: count-based compound topology pass (strictly additive) ---
    # Emit compound topologies when chip-count thresholds are met.  The `seen`
    # set carries over so we never duplicate an entry already added in pass 1.
    # Each newly added compound topology also expands its own superset subsets.

    def _add_compound(topology: str) -> None:
        """Add *topology* (and its superset subsets) to result if not already seen."""
        if topology not in seen:
            seen.add(topology)
            result.append(topology)
            for extra in _SUPERSET_INCLUDES.get(topology, []):
                if extra not in seen:
                    seen.add(extra)
                    result.append(extra)

    n150_count   = raw_counts.get("n150", 0)
    galaxy_count = raw_counts.get("galaxy", 0)
    t3k_count    = raw_counts.get("t3000", 0)

    # 4× n150 chips → N150X4 compound topology
    if n150_count >= 4:
        _add_compound("N150X4")

    # Galaxy multi-system compound topologies
    # galaxy_count == 1  → single GALAXY already emitted in pass 1
    # galaxy_count == 3  → not a supported topology; falls through silently (no compound emitted)
    if galaxy_count >= 4:
        _add_compound("QUAD_GALAXY")
    elif galaxy_count == 2:
        # galaxy racks come in 2- or 4-unit configurations; count == 3 is not a supported topology
        _add_compound("DUAL_GALAXY")

    # Mixed Galaxy + T3K compound topology
    if galaxy_count >= 1 and t3k_count >= 1:
        _add_compound("GALAXY_T3K")

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


def _extract_temp(telemetry: dict) -> Optional[float]:
    """Pull ASIC temperature from a telemetry dict (handles multiple tt-smi schema variants)."""
    for key in ("asic_temperature", "board_temperature", "smbus_tx_data"):
        val = telemetry.get(key)
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, dict):
            # tt-smi 5.x nests data under smbus_tx_data
            for inner_key in ("asic_temperature", "board_temperature", "local_temperature"):
                inner = val.get(inner_key)
                if isinstance(inner, (int, float)):
                    return float(inner)
    return None


def _extract_aiclk(telemetry: dict) -> Optional[int]:
    """Pull AI clock MHz from a telemetry dict."""
    for key in ("aiclk", "ai_clk", "clock"):
        val = telemetry.get(key)
        if isinstance(val, (int, float)):
            return int(val)
    # Also search inside nested sub-dicts (e.g. smbus_tx_data in tt-smi 5.x)
    for val in telemetry.values():
        if isinstance(val, dict):
            for inner_key in ("aiclk", "ai_clk"):
                inner = val.get(inner_key)
                if isinstance(inner, (int, float)):
                    return int(inner)
    return None


def get_chip_statuses(json_str: str) -> List[ChipStatus]:
    """Parse tt-smi -s JSON and return per-chip telemetry snapshots."""
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        return []

    chips = []
    for idx, entry in enumerate(data.get("device_info", [])):
        board_info = entry.get("board_info") if isinstance(entry.get("board_info"), dict) else {}
        bt = board_info.get("board_type") or entry.get("board_type", "unknown")

        telemetry = entry.get("telemetry", {})
        if not isinstance(telemetry, dict):
            telemetry = {}

        fw_status = entry.get("fw_status", {})
        if not isinstance(fw_status, dict):
            fw_status = {}
        fw_ver = fw_status.get("fw_version") or fw_status.get("version") or entry.get("fw_version", "")

        chips.append(ChipStatus(
            index=idx,
            board_type=bt,
            temp_c=_extract_temp(telemetry),
            aiclk_mhz=_extract_aiclk(telemetry),
            fw_version=str(fw_ver),
        ))
    return chips


def get_chip_statuses_live() -> List[ChipStatus]:
    """Run tt-smi -s and return per-chip telemetry. Returns [] on failure."""
    try:
        out = subprocess.run(
            ["tt-smi", "-s"], capture_output=True, text=True, timeout=10
        )
        return get_chip_statuses(out.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
