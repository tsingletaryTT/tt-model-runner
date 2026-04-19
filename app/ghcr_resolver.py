#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Resolve GHCR image tags.

When a specific tag is not found, queries the GHCR OCI v2 API to find the
latest available tag with the same version prefix, then falls back to the
most recent tag overall. Only handles ghcr.io registries.
"""
import json
import re
import urllib.request
import urllib.error
from typing import List, Optional, Tuple


def parse_image_ref(ref: str) -> Tuple[str, str, str]:
    """Parse a full image reference into (registry, image_path, tag).

    Examples:
      "ghcr.io/tenstorrent/tt-media-inference-server:0.12.0-2508216"
        → ("ghcr.io", "tenstorrent/tt-media-inference-server", "0.12.0-2508216")
      "ghcr.io/tenstorrent/tt-inference-server/vllm-tt-metal:v0.56.0"
        → ("ghcr.io", "tenstorrent/tt-inference-server/vllm-tt-metal", "v0.56.0")
    """
    last_segment = ref.split("/")[-1]
    if ":" in last_segment:
        tag = last_segment.split(":")[-1]
        without_tag = ref.rsplit(":", 1)[0]
    else:
        tag = ""
        without_tag = ref

    slash_idx = without_tag.find("/")
    if slash_idx == -1:
        return "docker.io", without_tag, tag

    registry = without_tag[:slash_idx]
    image_path = without_tag[slash_idx + 1:]
    return registry, image_path, tag


def _get_ghcr_token(image_path: str) -> Optional[str]:
    """Get an anonymous GHCR pull token for the given image path."""
    url = (
        f"https://ghcr.io/token?service=ghcr.io"
        f"&scope=repository:{image_path}:pull"
    )
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get("token") or data.get("access_token")
    except Exception:
        return None


def _list_tags(image_path: str, token: str) -> List[str]:
    """Return all tags listed by GHCR v2 API for image_path."""
    url = f"https://ghcr.io/v2/{image_path}/tags/list"
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get("tags") or []
    except Exception:
        return []


def _best_tag(tags: List[str], failed_tag: str) -> Optional[str]:
    """Pick the most suitable tag from the available list.

    Strategy (in order):
    1. Same version prefix (e.g. "0.12.0" from "0.12.0-2508216"), highest build suffix.
    2. Highest numeric-looking tag overall.
    3. "latest" as last resort.
    """
    if not tags:
        return None

    versioned = [t for t in tags if t != "latest"]
    if not versioned:
        return "latest" if "latest" in tags else None

    prefix_m = re.match(r'^([\d.]+)-', failed_tag)
    if prefix_m:
        prefix = prefix_m.group(1)
        same_prefix = [t for t in versioned if t.startswith(prefix + "-")]
        if same_prefix:
            def _build_num(t: str) -> int:
                m = re.search(r'-(\d+)$', t)
                return int(m.group(1)) if m else 0
            same_prefix.sort(key=_build_num, reverse=True)
            best = same_prefix[0]
            if best != failed_tag:
                return best

    # Fall back: sort by all numeric components descending
    def _numeric_key(t: str) -> List[int]:
        return [int(n) for n in re.findall(r'\d+', t)] or [0]

    versioned.sort(key=_numeric_key, reverse=True)
    best = versioned[0]
    return best if best != failed_tag else None


def resolve_latest_tag(image_ref: str) -> Optional[str]:
    """Return a new image ref with the best available tag, or None on failure.

    Only handles ghcr.io. Returns None if resolution fails or the resolved tag
    is the same as the one that already failed.
    """
    registry, image_path, failed_tag = parse_image_ref(image_ref)
    if not registry.endswith("ghcr.io"):
        return None

    token = _get_ghcr_token(image_path)
    if not token:
        return None

    tags = _list_tags(image_path, token)
    best = _best_tag(tags, failed_tag)
    if not best:
        return None

    return f"{registry}/{image_path}:{best}"
