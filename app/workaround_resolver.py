#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Data-driven known-issue workarounds.

Pure logic — NO GTK / Textual / controller imports.  Matches a device+model
(and optionally a crash log line) against a bundled knowledge base and returns
the workarounds that apply.  See docs/superpowers/specs/2026-07-01-*.md.
"""
import json
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
