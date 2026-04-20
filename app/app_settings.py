#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Persist user preferences to ~/.config/tt-runner-gui/settings.json."""
import json
from pathlib import Path

_DEFAULTS = {
    "server_repo_path": str(Path.home() / "code" / "tt-inference-server"),
    "last_model": "",
    "last_device": "",
    "last_port": "8000",
    "tree_expanded_types": ["LLM"],
    # Active model type filters — empty list means all types shown.
    "type_filters": [],
    "log_level_filters": ["DEBUG", "INFO", "WARN", "ERROR"],
    "window_width": 1280,
    "window_height": 820,
    "window_maximized": False,
    "sidebar_width": 290,
    # Tracks the last successfully-initiated launch so cross-engine resets can be suggested.
    "last_launched_engine": "",
    "last_launched_model_display": "",
    # Unix timestamp of the last LAUNCHING transition — used to detect reboots.
    "last_launched_at": 0.0,
    # Path to the tt-developer-image repo (for tt-forge/tt-metal model scripts).
    "dev_image_repo_path": str(Path.home() / "code" / "tt-developer-image"),
    # Last 5 launched models: [{"model_name": str, "device": str, "display": str}]
    "recent_models": [],
    # Starred/pinned models: [{"model_name": str, "device": str}]
    "starred_models": [],
    # Last 50 benchmark results: [{"model_name", "device", "timestamp", "isl", "osl",
    #   "concurrency", "mean_ttft_ms", "p95_ttft_ms", "mean_tps", "tps_decode",
    #   "mean_e2el_ms", "request_throughput", "tier_pass"}]
    "benchmark_history": [],
    # Per-model launch option overrides: {model_name: {field: value, ...}}
    # Only non-default fields are stored. Merged on top of the use-case preset when
    # the user selects that model again.
    "model_options": {},
    # HuggingFace token — stored here so it survives shell restarts.
    # Written to ~/.huggingface/token on save so hf libraries pick it up automatically.
    "hf_token": "",
    # Host directory bind-mounted into Docker containers as CACHE_ROOT.
    # Auto-created on first launch if it doesn't exist.
    "cache_root_path": str(Path.home() / ".cache" / "tt-model-runner"),
}


class AppSettings:
    def __init__(self, config_dir=None):
        if config_dir:
            self._path = Path(config_dir) / "settings.json"
        else:
            self._path = Path.home() / ".config" / "tt-runner-gui" / "settings.json"
        self._data = dict(_DEFAULTS)
        if self._path.exists():
            try:
                loaded = json.loads(self._path.read_text())
                self._data.update({k: v for k, v in loaded.items() if k in _DEFAULTS})
            except (json.JSONDecodeError, OSError):
                pass

    def save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2))

    def set_hf_token(self, token: str) -> None:
        """Persist token in settings and write to ~/.huggingface/token (hf library standard)."""
        self.hf_token = token
        self.save()
        hf_dir = Path.home() / ".huggingface"
        hf_dir.mkdir(parents=True, exist_ok=True)
        if token:
            (hf_dir / "token").write_text(token)
        elif (hf_dir / "token").exists():
            (hf_dir / "token").unlink(missing_ok=True)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            super().__setattr__(name, value)
        else:
            self._data[name] = value


settings = AppSettings()
