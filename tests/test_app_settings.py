import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
from app_settings import AppSettings


def test_defaults(tmp_path):
    s = AppSettings(config_dir=tmp_path)
    assert s.last_port == "8000"
    assert s.log_level_filters == ["DEBUG", "INFO", "WARN", "ERROR"]
    assert s.window_width == 1280


def test_roundtrip(tmp_path):
    s = AppSettings(config_dir=tmp_path)
    s.last_model = "meta-llama/Llama-3.2-1B"
    s.last_device = "N150"
    s.save()
    s2 = AppSettings(config_dir=tmp_path)
    assert s2.last_model == "meta-llama/Llama-3.2-1B"
    assert s2.last_device == "N150"


def test_invalid_json_falls_back(tmp_path):
    (tmp_path / "settings.json").write_text("not json{{{")
    s = AppSettings(config_dir=tmp_path)
    assert s.last_port == "8000"


def test_partial_file_keeps_defaults(tmp_path):
    (tmp_path / "settings.json").write_text('{"last_port": "9000"}')
    s = AppSettings(config_dir=tmp_path)
    assert s.last_port == "9000"
    assert s.window_width == 1280
