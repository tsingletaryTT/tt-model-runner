#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Data-driven known-issue workarounds.

Pure logic — NO GTK / Textual / controller imports.  Matches a device+model
(and optionally a crash log line) against a bundled knowledge base and returns
the workarounds that apply.  See docs/superpowers/specs/2026-07-01-*.md.
"""
import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

_DEFAULT_KB = Path(__file__).resolve().parent.parent / "data" / "workarounds.json"


@dataclass
class Workaround:
    id: str
    devices: List[str] = field(default_factory=list)
    models: List[str] = field(default_factory=list)
    symptom: Optional[str] = None
    env: dict = field(default_factory=dict)
    vllm: dict = field(default_factory=dict)
    also_move_to_env: List[str] = field(default_factory=list)
    auto: bool = True
    tradeoff: str = ""
    ref: str = ""
    applies_to_versions: Optional[str] = None


def load_workarounds(path: Optional[Path] = None) -> List[Workaround]:
    """Load and parse the knowledge base. Returns [] on any read/parse error."""
    p = Path(path) if path else _DEFAULT_KB
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    out: List[Workaround] = []
    for obj in raw:
        if not isinstance(obj, dict) or "id" not in obj:
            continue
        out.append(Workaround(
            id=obj["id"],
            devices=list(obj.get("devices", [])),
            models=list(obj.get("models", [])),
            symptom=obj.get("symptom"),
            env=dict(obj.get("env", {})),
            vllm=dict(obj.get("vllm", {})),
            also_move_to_env=list(obj.get("also_move_to_env", [])),
            auto=bool(obj.get("auto", True)),
            tradeoff=obj.get("tradeoff", ""),
            ref=obj.get("ref", ""),
            applies_to_versions=obj.get("applies_to_versions"),
        ))
    return out


def _glob_match(pattern: str, value: str) -> bool:
    """Match pattern against value, case-insensitive."""
    return fnmatch.fnmatch(value.lower(), pattern.lower())


def _version_applies(constraint: Optional[str], version: Optional[str]) -> bool:
    """True if `version` satisfies `constraint` (e.g. '<=0.10.0'). Fail open."""
    if not constraint or not version:
        return True
    ops = [("<=", lambda a, b: a <= b), (">=", lambda a, b: a >= b),
           ("==", lambda a, b: a == b), ("<", lambda a, b: a < b),
           (">", lambda a, b: a > b)]
    for prefix, cmp in ops:
        if constraint.startswith(prefix):
            target = constraint[len(prefix):].strip()
            try:
                va = tuple(int(x) for x in version.lstrip("v").split("."))
                vb = tuple(int(x) for x in target.lstrip("v").split("."))
            except ValueError:
                return True  # unparseable → fail open
            # pad to equal length so (0, 10) vs (0, 10, 0) compares correctly
            n = max(len(va), len(vb))
            va += (0,) * (n - len(va))
            vb += (0,) * (n - len(vb))
            return cmp(va, vb)
    return True  # unrecognized constraint syntax → fail open


def _device_matches(w: "Workaround", device: str) -> bool:
    """True if workaround applies to any device (empty list) or device is in the list."""
    return not w.devices or device.upper() in {d.upper() for d in w.devices}


def _model_matches(w: "Workaround", model: str) -> bool:
    """True if workaround applies to any model (empty list) or model matches a glob pattern."""
    return not w.models or any(_glob_match(p, model) for p in w.models)


def match_preflight(device: str, model: str,
                    repo_version: Optional[str] = None,
                    kb: Optional[List["Workaround"]] = None) -> List["Workaround"]:
    """Return all workarounds matching device, model, and version (ignoring symptom)."""
    items = kb if kb is not None else load_workarounds()
    return [w for w in items
            if _device_matches(w, device)
            and _model_matches(w, model)
            and _version_applies(w.applies_to_versions, repo_version)]


def match_symptom(log_line: str, device: str, model: str,
                  kb: Optional[List["Workaround"]] = None) -> Optional["Workaround"]:
    """Return the first workaround whose device+model match AND whose symptom regex hits the log line."""
    items = kb if kb is not None else load_workarounds()
    for w in items:
        if not w.symptom:
            continue
        if not (_device_matches(w, device) and _model_matches(w, model)):
            continue
        if re.search(w.symptom, log_line, re.I):
            return w
    return None
