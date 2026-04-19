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
    "log_level_filters": ["DEBUG", "INFO", "WARN", "ERROR"],
    "window_width": 1280,
    "window_height": 820,
    "sidebar_width": 290,
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
