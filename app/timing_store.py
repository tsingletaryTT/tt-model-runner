#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Persistent load timing estimates with cross-model inference cascade.

Estimation cascade (estimate_load):
  1. Exact match — own key has ≥1 sample → trimmed mean
  2. Family + device rate × size_gb
  3. Device baseline rate × size_gb
  4. Cross-device scale from nearest known device
  5. None — return confidence="none"
"""
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Literal, Optional

_DEFAULT_PATH = Path.home() / ".config" / "tt-runner-gui" / "timing.json"
_MAX_SAMPLES = 10

# Cross-device throughput tier ratios relative to N150 (larger = faster)
_DEVICE_TIER = {
    "N150":   1.0,
    "P150":   0.9,
    "P300":   0.5,
    "P150X4": 0.25,
    "P300X2": 0.25,
    "T3K":    0.22,
}

# Pre-seeded from real logs on this machine (QB2, 2026-04-18)
_BOOTSTRAP: Dict = {
    "schema_version": 1,
    "load_samples": {
        "Wan-AI/Wan2.2-T2V-A14B-Diffusers_P300X2_warm":     [151, 177, 151, 137, 151, 145, 152],
        "Wan-AI/Wan2.2-Animate-14B-Diffusers_P300X2_cold":   [218],
        "meta-llama/Llama-3.1-8B-Instruct_P150_cold":        [100, 100],
        "meta-llama/Llama-3.1-8B-Instruct_P150_warm":        [12, 13, 20, 60],
    },
    "substage_samples": {
        "Wan-AI/Wan2.2-T2V-A14B-Diffusers_P300X2_device_init":   [13, 14, 15, 15, 13, 15, 15],
        "Wan-AI/Wan2.2-T2V-A14B-Diffusers_P300X2_cache_loading":  [16, 16, 6, 15, 14, 16],
        "Wan-AI/Wan2.2-T2V-A14B-Diffusers_P300X2_warmup":         [116, 138, 113, 112, 116, 111, 115],
    },
    "device_load_rate": {
        "P150":   {"seconds_per_gb": 7.23, "sample_count": 7},
        "P300X2": {"seconds_per_gb": 5.5,  "sample_count": 7},
    },
    "family_load_rate": {
        "Llama_P150": {"seconds_per_gb": 6.35, "sample_count": 6},
        "Wan_P300X2": {"seconds_per_gb": 5.5,  "sample_count": 7},
    },
    "download_speed_mbps": [],
}


@dataclass
class EstimateResult:
    seconds: Optional[float]
    confidence: Literal["none", "low", "medium", "high"]
    source: str


def _trimmed_mean(samples: List[float]) -> float:
    if len(samples) <= 2:
        return statistics.mean(samples)
    s = sorted(samples)
    trim = max(1, len(s) // 5)
    return statistics.mean(s[trim:-trim])


def _deep_copy(obj):
    return json.loads(json.dumps(obj))


class TimingStore:
    def __init__(self, path: Path = _DEFAULT_PATH):
        self._path = Path(path)
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError):
                self._data = _deep_copy(_BOOTSTRAP)
        else:
            self._data = _deep_copy(_BOOTSTRAP)
            self.save()

    def save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2))

    def _append_sample(self, section: str, key: str, value: float):
        self._data.setdefault(section, {}).setdefault(key, [])
        samples = self._data[section][key]
        samples.append(value)
        if len(samples) > _MAX_SAMPLES:
            self._data[section][key] = samples[-_MAX_SAMPLES:]

    def record_load(self, hf_repo: str, device: str, duration_s: float, cold: bool):
        key = f"{hf_repo}_{device.upper()}_{'cold' if cold else 'warm'}"
        self._append_sample("load_samples", key, duration_s)
        self.save()

    def record_substage(self, hf_repo: str, device: str, stage: str, duration_s: float):
        key = f"{hf_repo}_{device.upper()}_{stage}"
        self._append_sample("substage_samples", key, duration_s)
        self.save()

    def estimate_load(
        self, hf_repo: str, device: str, cold: bool, size_gb: float, family: str
    ) -> EstimateResult:
        temp = "cold" if cold else "warm"
        dev = device.upper()
        key = f"{hf_repo}_{dev}_{temp}"

        # 1. Exact match
        samples = self._data.get("load_samples", {}).get(key, [])
        if samples:
            n = len(samples)
            est = _trimmed_mean(samples)
            conf = "high" if n >= 3 else "medium" if n >= 2 else "low"
            return EstimateResult(est, conf, f"{hf_repo.split('/')[-1]} on {dev} ({n} samples)")

        # 2. Family + device rate
        fam_key = f"{family}_{dev}"
        fam_rate = self._data.get("family_load_rate", {}).get(fam_key)
        if fam_rate and size_gb:
            est = fam_rate["seconds_per_gb"] * size_gb
            if cold:
                est *= 5.0
            n = fam_rate["sample_count"]
            return EstimateResult(est, "medium", f"{family} family on {dev} ({n} samples)")

        # 3. Device baseline
        dev_rate = self._data.get("device_load_rate", {}).get(dev)
        if dev_rate and size_gb:
            est = dev_rate["seconds_per_gb"] * size_gb
            if cold:
                est *= 5.0
            return EstimateResult(est, "low", f"{dev} device baseline")

        # 4. Cross-device scale
        if size_gb and dev in _DEVICE_TIER:
            for other_dev, other_rate in self._data.get("device_load_rate", {}).items():
                if other_dev in _DEVICE_TIER and other_dev != dev:
                    scale = _DEVICE_TIER[dev] / _DEVICE_TIER[other_dev]
                    est = other_rate["seconds_per_gb"] * size_gb * scale
                    if cold:
                        est *= 5.0
                    return EstimateResult(est, "low", f"scaled from {other_dev}")

        return EstimateResult(None, "none", "no data")

    def estimate_substage(self, hf_repo: str, device: str, stage: str) -> EstimateResult:
        key = f"{hf_repo}_{device.upper()}_{stage}"
        samples = self._data.get("substage_samples", {}).get(key, [])
        if not samples:
            return EstimateResult(None, "none", "no substage data")
        n = len(samples)
        est = _trimmed_mean(samples)
        conf = "high" if n >= 5 else "medium" if n >= 2 else "low"
        return EstimateResult(est, conf, f"{stage} ({n} samples)")
