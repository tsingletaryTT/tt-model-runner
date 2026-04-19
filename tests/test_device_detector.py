import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
from device_detector import board_type_to_device, detect_devices_from_json

QB2_JSON = '{"device_info": [{"board_type": "p300c", "index": 0}, {"board_type": "p300c", "index": 1}, {"board_type": "p300c", "index": 2}, {"board_type": "p300c", "index": 3}]}'


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
