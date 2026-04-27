#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Persist SavedConfig to ~/.config/tt-runner-gui/saved_configs/.

Public API
----------
save_last_success(config)  -- overwrite __last_success__.json
load_last_success()        -- return SavedConfig or None
save_named(config)         -- write <name>.json (config.name must not be sentinel)
load_named(name)           -- return SavedConfig; raises FileNotFoundError if absent
list_named()               -- sorted list of names (excludes __last_success__)
delete_named(name)         -- remove <name>.json; raises ValueError for sentinel

File layout
-----------
Each config is stored as a single JSON file:
    <_SAVED_CONFIGS_DIR>/<name>.json

The special name ``__last_success__`` is reserved for the auto-saved record of
the most recent successful launch.  User-chosen names must match the regex
``^[\\w\\-. ]+$`` and may not equal the sentinel.

Atomic writes
-------------
Every write goes through ``_write()``, which writes to a ``.tmp`` file first
and then renames it into place.  On POSIX systems ``rename()`` is atomic, so
readers never see a partial file.
"""
import json
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from saved_config import SavedConfig

# ---------------------------------------------------------------------------
# Module-level constant; monkeypatched in tests via scs._SAVED_CONFIGS_DIR.
# ---------------------------------------------------------------------------
_SAVED_CONFIGS_DIR = Path.home() / ".config" / "tt-runner-gui" / "saved_configs"

# The reserved config name used for the last-success auto-save.
_SENTINEL = "__last_success__"

# Valid characters for user-chosen config names (letters, digits, _, -, ., space).
_NAME_RE = re.compile(r"^[\w\-. ]+$")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _config_path(name: str) -> Path:
    """Return the absolute path for a config JSON file.

    Performs a path-traversal guard: the resolved path must live inside
    ``_SAVED_CONFIGS_DIR``.  The guard reads the *current* value of the module
    attribute so monkeypatching in tests works correctly.

    Raises
    ------
    ValueError
        If the resolved path escapes the base directory (e.g. ``../evil``).
    """
    base = _SAVED_CONFIGS_DIR
    candidate = (base / f"{name}.json").resolve()
    if base.resolve() not in candidate.parents:
        raise ValueError(f"Invalid config name: {name!r}")
    # Return the un-resolved path so callers see the same base they passed in.
    return base / f"{name}.json"


def _dict_to_config(data: dict) -> SavedConfig:
    """Deserialise a raw dict into SavedConfig, ignoring unknown keys.

    Unknown keys (e.g. from a future schema version) are silently dropped so
    old code can still load newer config files.
    """
    known = set(SavedConfig.__dataclass_fields__)
    return SavedConfig(**{k: v for k, v in data.items() if k in known})


def _write(path: Path, data: dict) -> None:
    """Atomically write *data* as JSON to *path*.

    Writes to a ``.tmp`` sibling first, then renames into place.  On POSIX
    systems ``rename()`` is an atomic operation so readers never observe a
    partial or empty file.
    """
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save_last_success(config: SavedConfig) -> None:
    """Overwrite ``__last_success__.json`` with *config*.

    The ``name`` field in the written JSON is forced to ``_SENTINEL``
    regardless of ``config.name``.  ``created`` is set on the first save;
    ``last_used`` is updated on every call.  The *config* object itself is
    never mutated.
    """
    _SAVED_CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    # Work on a copy of the dict — never mutate the caller's object.
    data = asdict(config)
    data["name"] = _SENTINEL
    now = datetime.now().isoformat(timespec="seconds")
    if not data["created"]:
        data["created"] = now
    data["last_used"] = now
    _write(_config_path(_SENTINEL), data)


def load_last_success() -> Optional[SavedConfig]:
    """Return the last-success config, or ``None`` if no successful launch yet.

    Returns ``None`` instead of raising if the file is absent, unreadable, or
    corrupt — this is an expected state (first run, or file was deleted).
    """
    path = _config_path(_SENTINEL)
    if not path.exists():
        return None
    try:
        return _dict_to_config(json.loads(path.read_text()))
    except (json.JSONDecodeError, OSError, TypeError, KeyError):
        return None


def save_named(config: SavedConfig) -> None:
    """Write a user-named config to disk.

    ``config.name`` must:
    - Not equal the sentinel (``__last_success__``).
    - Match ``^[\\w\\-. ]+$`` (path-safe characters only).

    The path-traversal guard in ``_config_path`` provides a second layer of
    defence against names that would escape the config directory (e.g. ``../x``).

    ``created`` is set on the first save; ``last_used`` is updated on every
    call.  The *config* object itself is never mutated.

    Raises
    ------
    ValueError
        If ``config.name`` is the sentinel or contains invalid characters.
    """
    name = config.name
    if name == _SENTINEL:
        raise ValueError(f"Cannot save with reserved name {_SENTINEL!r}")
    if not _NAME_RE.match(name):
        raise ValueError(f"Invalid config name: {name!r}")
    _SAVED_CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    # Work on a copy of the dict — never mutate the caller's object.
    data = asdict(config)
    now = datetime.now().isoformat(timespec="seconds")
    if not data["created"]:
        data["created"] = now
    data["last_used"] = now
    _write(_config_path(name), data)


def load_named(name: str) -> SavedConfig:
    """Return the named config.

    Raises
    ------
    FileNotFoundError
        If no config with the given name exists on disk.
    """
    path = _config_path(name)
    if not path.exists():
        raise FileNotFoundError(f"No saved config named {name!r}")
    return _dict_to_config(json.loads(path.read_text()))


def list_named() -> list:
    """Return a sorted list of user-named config names.

    Excludes ``__last_success__`` so callers always get only the names that
    were saved via ``save_named()``.  Returns an empty list if the directory
    does not exist yet (first run).
    """
    if not _SAVED_CONFIGS_DIR.exists():
        return []
    return sorted(
        p.stem for p in _SAVED_CONFIGS_DIR.glob("*.json")
        if p.stem != _SENTINEL
    )


def delete_named(name: str) -> None:
    """Delete a named config file.

    The sentinel name ``__last_success__`` cannot be deleted via this function
    — use ``save_last_success`` to overwrite it, or remove the file manually.

    Does nothing silently if the named config does not exist (idempotent).

    Raises
    ------
    ValueError
        If *name* is the sentinel ``__last_success__``.
    """
    if name == _SENTINEL:
        raise ValueError(f"Cannot delete reserved config {_SENTINEL!r}")
    path = _config_path(name)
    if path.exists():
        path.unlink()
