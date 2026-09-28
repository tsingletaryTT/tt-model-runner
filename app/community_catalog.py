#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Discover tt-model-manager community bundles via `tt model list --community --all`.

Community bundles are self-contained (no docker_image, no model_spec-style
device_type) — this module maps what's available onto ModelEntry so the rest
of the app (filtering, tree building, launching) doesn't need to know the
difference beyond checking `source`. Field mapping matches tt-cli's real
`BundleInfo` dataclass (modelhub/bundles.py): name, hardware (list of
board/mesh tags), engine, weights_repo — verified against the live
~/code/tt-cli checkout on 2026-09-28.
"""
import json
import shutil
import subprocess
import threading
from typing import Callable, List, Optional

from model_catalog import ModelEntry

_TIMEOUT = 15

# Maps tt-cli hardware tags (lowercase) that don't already match
# model_spec.json's device_type vocabulary as a plain uppercase (e.g.
# "p300x2" -> "P300X2" needs no entry here). Only real aliases need listing.
_DEVICE_ALIASES = {
    "t3000": "T3K",
    "t3k": "T3K",
    "galaxy": "GALAXY",
}


def _map_device_type(hardware: list) -> str:
    if not hardware:
        return "UNKNOWN"
    tag = str(hardware[0]).lower()
    return _DEVICE_ALIASES.get(tag, tag.upper())


def _entry_from_record(rec: dict) -> ModelEntry:
    bundle_id = rec.get("name") or rec.get("id") or rec.get("bundle_id") or ""
    display = bundle_id.split("/")[-1] if "/" in bundle_id else bundle_id
    org = bundle_id.split("/")[0] if "/" in bundle_id else "community"
    hardware = rec.get("hardware") or []
    if isinstance(hardware, str):
        hardware = [hardware]
    engine = rec.get("engine")
    if isinstance(engine, list):
        engine = engine[0] if engine else None
    return ModelEntry(
        model_id=bundle_id,
        model_name=bundle_id,
        display_name=display,
        hf_model_repo=rec.get("weights_repo") or rec.get("weights") or bundle_id,
        model_type="COMMUNITY",
        family=org,
        device_type=_map_device_type(hardware),
        inference_engine=(engine or "vllm").lower(),
        docker_image="",
        status="COMMUNITY",
        param_count=None,
        min_disk_gb=None,
        min_ram_gb=None,
        source="community",
    )


def parse_community_list(raw: str) -> List[ModelEntry]:
    """Parse `tt model list --community --all` output into ModelEntry objects.

    Tries JSON first (`{"models": [...]}`, tt-cli's real shape — a bare list
    is also accepted defensively); falls back to a whitespace-separated text
    table (first column is the bundle id) if --json is ever unavailable.
    """
    raw = raw.strip()
    if not raw:
        return []

    try:
        data = json.loads(raw)
        records = data if isinstance(data, list) else data.get("models", [])
        return [_entry_from_record(rec) for rec in records]
    except (json.JSONDecodeError, AttributeError):
        pass

    entries: List[ModelEntry] = []
    for line in raw.splitlines():
        line = line.strip()
        parts = line.split()
        if not parts or "/" not in parts[0]:
            continue
        entries.append(_entry_from_record({"name": parts[0]}))
    return entries


def _run_tt(args: List[str]) -> Optional[str]:
    tt_bin = shutil.which("tt")
    if not tt_bin:
        return None
    try:
        result = subprocess.run(
            [tt_bin] + args, capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def fetch_community_entries() -> List[ModelEntry]:
    """Shell `tt model list --community --all` and parse the result.

    `--all` bypasses tt-cli's own hardware auto-detection filtering — this
    app's Community section always shows every community bundle regardless
    of this machine's detected hardware (see spec). Tries --json first; if
    that flag isn't supported (or any other failure), retries without it and
    parses the text-table fallback. Returns [] if the `tt` CLI isn't
    installed or both attempts fail — an empty community list is "nothing
    to show", not an error the caller needs to surface.
    """
    if not shutil.which("tt"):
        return []
    out = _run_tt(["model", "list", "--community", "--all", "--json"])
    if out is None:
        out = _run_tt(["model", "list", "--community", "--all"])
    if out is None:
        return []
    return parse_community_list(out)


def load_async(on_done: Callable[[List[ModelEntry]], None]) -> None:
    """Fetch community bundles in a background thread.

    on_done(entries) — entries is [] on any failure (missing CLI, timeout,
    parse failure). Called from a background thread — caller dispatches to
    the UI event loop if needed (mirrors compat_catalog.load_async).
    """
    def _run():
        try:
            on_done(fetch_community_entries())
        except Exception:
            on_done([])

    threading.Thread(target=_run, daemon=True).start()
