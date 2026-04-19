import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
from timing_store import TimingStore


def test_bootstrap_exact_match(tmp_path):
    store = TimingStore(tmp_path / "timing.json")
    r = store.estimate_load("Wan-AI/Wan2.2-T2V-A14B-Diffusers", "P300X2", cold=False, size_gb=37.4, family="Wan")
    assert r.confidence in ("medium", "high")
    assert r.seconds is not None
    assert 130 < r.seconds < 200


def test_device_baseline_fallback(tmp_path):
    store = TimingStore(tmp_path / "timing.json")
    r = store.estimate_load("meta-llama/Llama-3.2-1B", "P150", cold=False, size_gb=2.0, family="Llama")
    assert r.seconds is not None
    assert r.confidence != "none"


def test_record_improves_confidence(tmp_path):
    path = tmp_path / "timing.json"
    store = TimingStore(path)
    store.record_load("meta-llama/Llama-3.2-1B", "N150", 95.0, cold=False)
    store.record_load("meta-llama/Llama-3.2-1B", "N150", 90.0, cold=False)
    store.record_load("meta-llama/Llama-3.2-1B", "N150", 92.0, cold=False)
    r = store.estimate_load("meta-llama/Llama-3.2-1B", "N150", cold=False, size_gb=2.0, family="Llama")
    assert r.confidence == "high"
    assert 88 < r.seconds < 98


def test_fifo_eviction(tmp_path):
    path = tmp_path / "timing.json"
    store = TimingStore(path)
    for i in range(15):
        store.record_load("test/model", "N150", float(i * 10), cold=False)
    data = json.loads(path.read_text())
    assert len(data["load_samples"]["test/model_N150_warm"]) == 10


def test_substage_estimate(tmp_path):
    store = TimingStore(tmp_path / "timing.json")
    r = store.estimate_substage("Wan-AI/Wan2.2-T2V-A14B-Diffusers", "P300X2", "warmup")
    assert r.seconds is not None
    assert 110 < r.seconds < 120
    assert r.confidence == "high"


def test_no_data_returns_none(tmp_path):
    store = TimingStore(tmp_path / "timing.json")
    r = store.estimate_load("unknown/model", "GALAXY", cold=False, size_gb=100.0, family="Unknown")
    assert r.confidence == "none"
    assert r.seconds is None
