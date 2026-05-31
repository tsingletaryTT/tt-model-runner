import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
from device_detector import board_type_to_device, detect_devices_from_json

QB2_JSON = '{"device_info": [{"board_type": "p300c", "index": 0}, {"board_type": "p300c", "index": 1}, {"board_type": "p300c", "index": 2}, {"board_type": "p300c", "index": 3}]}'


def _make_json(chips: list) -> str:
    """Build tt-smi -s style JSON with nested board_info schema.

    Each element in *chips* is a (board_type, board_id) tuple.  The resulting
    JSON matches the tt-smi 5.x nested schema:
        device_info[i].board_info.board_type
    which is one of the two schema variants handled by detect_devices_from_json.
    """
    devices = []
    for bt, bid in chips:
        devices.append({
            "board_info": {"board_type": bt, "board_id": bid},
            "telemetry": {},
            "fw_status": {},
        })
    return json.dumps({"device_info": devices})


def test_board_type_mapping():
    assert board_type_to_device("p300c") == "P300X2"
    assert board_type_to_device("n150") == "N150"
    assert board_type_to_device("p150") == "P150"
    assert board_type_to_device("unknown") is None


def test_detect_qb2():
    devices = detect_devices_from_json(QB2_JSON)
    assert "P300X2" in devices
    assert "P300" in devices


def test_detect_single_n150():
    js = '{"device_info": [{"board_type": "n150", "index": 0}]}'
    assert detect_devices_from_json(js) == ["N150"]


def test_malformed_json_returns_empty():
    assert detect_devices_from_json("not json {{{{") == []


def test_double_json_objects():
    # tt-smi -s sometimes emits two concatenated objects; fallback extracts board_type via regex
    double = QB2_JSON + QB2_JSON
    devices = detect_devices_from_json(double)
    assert "P300X2" in devices


# ---------------------------------------------------------------------------
# Topology detection tests — nested board_info schema (tt-smi 5.x)
# ---------------------------------------------------------------------------

def test_n150x4_detected():
    """Four n150 chips should expose the N150X4 compound topology as well as N150."""
    js = _make_json([("n150", f"id-{i}") for i in range(4)])
    result = detect_devices_from_json(js)
    assert "N150X4" in result, f"N150X4 missing from {result}"
    assert "N150" in result, f"N150 missing from {result}"


def test_n150_single_no_n150x4():
    """A single n150 chip should not trigger the N150X4 compound topology."""
    js = _make_json([("n150", "id-0")])
    result = detect_devices_from_json(js)
    assert "N150" in result, f"N150 missing from {result}"
    assert "N150X4" not in result, f"N150X4 should not appear for a single chip: {result}"


def test_dual_galaxy_detected():
    """Two galaxy boards should produce DUAL_GALAXY and also expose GALAXY and P150X8."""
    js = _make_json([("galaxy", f"id-{i}") for i in range(2)])
    result = detect_devices_from_json(js)
    assert "DUAL_GALAXY" in result, f"DUAL_GALAXY missing from {result}"
    assert "GALAXY" in result, f"GALAXY missing from {result}"
    assert "P150X8" in result, f"P150X8 missing from {result}"


def test_quad_galaxy_detected():
    """Four galaxy boards should produce QUAD_GALAXY and also include DUAL_GALAXY and GALAXY."""
    js = _make_json([("galaxy", f"id-{i}") for i in range(4)])
    result = detect_devices_from_json(js)
    assert "QUAD_GALAXY" in result, f"QUAD_GALAXY missing from {result}"
    assert "DUAL_GALAXY" in result, f"DUAL_GALAXY missing from {result}"
    assert "GALAXY" in result, f"GALAXY missing from {result}"


def test_galaxy_single_no_dual():
    """A single galaxy board should not trigger DUAL_GALAXY or QUAD_GALAXY."""
    js = _make_json([("galaxy", "id-0")])
    result = detect_devices_from_json(js)
    assert "GALAXY" in result, f"GALAXY missing from {result}"
    assert "DUAL_GALAXY" not in result, f"DUAL_GALAXY should not appear for a single Galaxy: {result}"
    assert "QUAD_GALAXY" not in result, f"QUAD_GALAXY should not appear for a single Galaxy: {result}"


def test_galaxy_t3k_detected():
    """One galaxy + one t3000 chip should produce the GALAXY_T3K compound topology."""
    js = _make_json([("galaxy", "id-0"), ("t3000", "id-1")])
    result = detect_devices_from_json(js)
    assert "GALAXY_T3K" in result, f"GALAXY_T3K missing from {result}"
    assert "GALAXY" in result, f"GALAXY missing from {result}"
    assert "T3K" in result, f"T3K missing from {result}"


def test_qb2_unchanged():
    """Four p300c dies (QB2) should retain all BH-family subset topologies."""
    js = _make_json([("p300c", f"id-{i}") for i in range(4)])
    result = detect_devices_from_json(js)
    # Primary QB2 topology and its known subsets (from _SUPERSET_INCLUDES["P300X2"])
    for expected in ("P300X2", "P300", "P150", "P100", "P150X4"):
        assert expected in result, f"{expected} missing from QB2 result: {result}"
    # Galaxy compound topologies must NOT be triggered by BH chips
    assert "DUAL_GALAXY" not in result, f"DUAL_GALAXY should not appear for QB2: {result}"
    assert "BLACKHOLE_GALAXY" not in result, f"BLACKHOLE_GALAXY should not appear for QB2: {result}"


def test_blackhole_galaxy_gone():
    """BLACKHOLE_GALAXY is a stale topology name that must never appear in results."""
    # Use a QB2 configuration — this is the scenario where the old name might have
    # been incorrectly emitted before the topology map was updated.
    js = _make_json([("p300c", f"id-{i}") for i in range(4)])
    result = detect_devices_from_json(js)
    assert "BLACKHOLE_GALAXY" not in result, (
        f"Stale BLACKHOLE_GALAXY topology should never be emitted: {result}"
    )
