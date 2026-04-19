#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Profile persistence — save/load/list/delete named LaunchOptions profiles.

Profiles are stored as JSON files under ~/.config/tt-runner-gui/profiles/.
Each profile file: { name, description, model_type, created, options: {...} }.
"""
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from launch_options import LaunchOptions

_PROFILES_DIR = Path.home() / ".config" / "tt-runner-gui" / "profiles"


def save_profile(
    name: str,
    description: str,
    model_type: str,
    options: LaunchOptions,
) -> None:
    """Write profile to disk, overwriting if it already exists."""
    _PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "name": name,
        "description": description,
        "model_type": model_type,
        "created": datetime.now().isoformat(timespec="seconds"),
        "options": asdict(options),
    }
    (_PROFILES_DIR / f"{name}.json").write_text(json.dumps(data, indent=2))


def load_profile(name: str) -> Optional[dict]:
    """Return profile dict or None if not found / corrupt."""
    path = _PROFILES_DIR / f"{name}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def list_profiles(model_type: str = "") -> List[dict]:
    """Return all profiles, optionally filtered by model_type.

    Corrupt files are silently skipped.  Profiles with no model_type match any
    filter.
    """
    if not _PROFILES_DIR.exists():
        return []
    result = []
    for p in sorted(_PROFILES_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if model_type and data.get("model_type") and data["model_type"] != model_type:
            continue
        result.append(data)
    return result


def delete_profile(name: str) -> bool:
    """Delete profile file.  Returns True if deleted, False if not found."""
    path = _PROFILES_DIR / f"{name}.json"
    if not path.exists():
        return False
    path.unlink()
    return True


def profile_to_options(profile: dict) -> LaunchOptions:
    """Reconstruct a LaunchOptions from a loaded profile dict."""
    opts_data = profile.get("options", {})
    known = {f for f in LaunchOptions.__dataclass_fields__}
    filtered = {k: v for k, v in opts_data.items() if k in known}
    return LaunchOptions(**filtered)
