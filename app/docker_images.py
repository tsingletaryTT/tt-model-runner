#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Scan locally pulled Docker images and surface TT-relevant ones.

scan_local_images() returns quickly (sub-second); call it in a background
thread and post results back to the UI via GLib.idle_add.
"""
import re
import subprocess
from dataclasses import dataclass
from typing import List, Optional


# Substrings that identify a TT-related image repository
_TT_MARKERS = (
    "tenstorrent",
    "tt-inference",
    "tt-metal",
    "tt-media",
)


@dataclass
class DockerImage:
    repo_tag: str        # "ghcr.io/tenstorrent/.../vllm-tt-metal:v0.56.0"
    size_str: str        # "4.21 GB"
    created_str: str     # "3 days ago"
    is_tt: bool          # True when repo matches any _TT_MARKERS entry

    @property
    def short_tag(self) -> str:
        """Last path segment + tag, e.g. 'vllm-tt-metal:v0.56.0'."""
        repo, _, tag = self.repo_tag.rpartition(":")
        name = repo.split("/")[-1]
        return f"{name}:{tag}" if tag else name


def scan_local_images(spec_default: str = "") -> List[DockerImage]:
    """Return all locally-available TT Docker images, spec default first.

    Args:
        spec_default: The image ref from the model spec (may not be pulled yet).

    Returns:
        List of DockerImage objects sorted by creation date (newest first).
        If docker is not available or returns an error, returns [].
        If spec_default is given and not found locally, a placeholder entry
        with is_tt=True is prepended (labelled "(spec default · not pulled)").
    """
    try:
        result = subprocess.run(
            [
                "docker", "images",
                "--format", "{{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    if result.returncode != 0:
        return []

    images: List[DockerImage] = []
    for raw_line in result.stdout.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        parts = raw_line.split("\t")
        if len(parts) < 3:
            continue
        repo_tag, size_str, created_str = parts[0], parts[1], parts[2]
        if repo_tag.endswith(":<none>") or repo_tag == "<none>:<none>":
            continue
        is_tt = any(m in repo_tag.lower() for m in _TT_MARKERS)
        if is_tt:
            images.append(DockerImage(
                repo_tag=repo_tag,
                size_str=size_str,
                created_str=created_str,
                is_tt=True,
            ))

    # Promote spec default to front if found, otherwise prepend placeholder
    if spec_default:
        spec_base = spec_default.split(":")[0]
        found_idx = next(
            (i for i, img in enumerate(images) if img.repo_tag.split(":")[0] == spec_base),
            None,
        )
        if found_idx is not None:
            images.insert(0, images.pop(found_idx))
        else:
            images.insert(0, DockerImage(
                repo_tag=spec_default,
                size_str="—",
                created_str="not pulled",
                is_tt=True,
            ))

    return images
