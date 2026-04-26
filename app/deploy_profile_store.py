#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Persist DeployProfile to ~/.config/tt-runner-gui/deploy_profiles/.

Public API
----------
save_deploy_profile(profile)  -- write profile JSON to disk (overwrites)
load_deploy_profile(name)     -- return DeployProfile or None
list_deploy_profiles()        -- return all saved profiles; corrupt files skipped
delete_deploy_profile(name)   -- remove profile file; returns True/False
"""
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from deploy_profile import DeployProfile, ProfileSlot
from launch_options import LaunchOptions

# Module-level constant; monkeypatched by tests via dps._DEPLOY_PROFILES_DIR.
_DEPLOY_PROFILES_DIR = Path.home() / ".config" / "tt-runner-gui" / "deploy_profiles"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _profile_path(name: str) -> Path:
    """Return the absolute path for a profile JSON file.

    Performs a basic path-traversal guard: the resolved path must live inside
    _DEPLOY_PROFILES_DIR.  The guard uses the *current* value of the module
    attribute so monkeypatching in tests works correctly.
    """
    base = _DEPLOY_PROFILES_DIR
    candidate = (base / f"{name}.json").resolve()
    if base.resolve() not in candidate.parents:
        raise ValueError(f"Invalid profile name: {name!r}")
    # Return the un-resolved path so callers see the same base they passed in.
    return base / f"{name}.json"


def _slot_from_dict(d: dict) -> ProfileSlot:
    """Deserialise a raw dict into a ProfileSlot, ignoring unknown keys.

    Unknown keys in ``options`` (e.g. from a future version of LaunchOptions)
    are silently dropped so old code can still load newer profile files.
    """
    opts_data = d.get("options") or {}
    known_opts = set(LaunchOptions.__dataclass_fields__)
    opts = LaunchOptions(**{k: v for k, v in opts_data.items() if k in known_opts})

    # Build ProfileSlot kwargs excluding "options" (handled above).
    known_slot = set(ProfileSlot.__dataclass_fields__) - {"options"}
    kwargs = {k: v for k, v in d.items() if k in known_slot}
    kwargs["options"] = opts
    return ProfileSlot(**kwargs)


def _profile_from_dict(data: dict) -> DeployProfile:
    """Deserialise a raw dict into a DeployProfile, ignoring unknown top-level keys."""
    slots = [_slot_from_dict(s) for s in data.get("slots", [])]
    known = set(DeployProfile.__dataclass_fields__) - {"slots"}
    kwargs = {k: v for k, v in data.items() if k in known}
    kwargs["slots"] = slots
    return DeployProfile(**kwargs)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save_deploy_profile(profile: DeployProfile) -> None:
    """Write *profile* to disk as JSON, overwriting any existing file."""
    _DEPLOY_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    data = asdict(profile)
    if not data["created"]:
        data["created"] = datetime.now().isoformat(timespec="seconds")
    _profile_path(profile.name).write_text(json.dumps(data, indent=2))


def load_deploy_profile(name: str) -> Optional[DeployProfile]:
    """Return the named DeployProfile, or *None* if it does not exist or is corrupt."""
    path = _profile_path(name)
    if not path.exists():
        return None
    try:
        return _profile_from_dict(json.loads(path.read_text()))
    except (json.JSONDecodeError, OSError, TypeError, KeyError):
        return None


def list_deploy_profiles() -> List[DeployProfile]:
    """Return every saved DeployProfile in name order.

    Corrupt or unreadable JSON files are silently skipped so one bad entry
    does not prevent the others from loading.
    """
    if not _DEPLOY_PROFILES_DIR.exists():
        return []
    result = []
    for p in sorted(_DEPLOY_PROFILES_DIR.glob("*.json")):
        try:
            result.append(_profile_from_dict(json.loads(p.read_text())))
        except (json.JSONDecodeError, OSError, TypeError, KeyError):
            continue
    return result


def delete_deploy_profile(name: str) -> bool:
    """Delete the named profile file.

    Returns ``True`` if the file was deleted, ``False`` if it did not exist.
    """
    path = _profile_path(name)
    if not path.exists():
        return False
    path.unlink()
    return True
