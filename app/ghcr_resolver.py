#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Resolve GHCR image tags.

Resolution strategy (in order):
  1. GHCR OCI v2 tags/list — list all available tags, pick best match.
  2. GitHub Releases API  — find latest release tag, probe manifest directly.
  3. "latest" sentinel    — try the well-known :latest tag as last resort.

Only handles ghcr.io registries.
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
    """Return all tags listed by GHCR v2 API, following pagination links."""
    tags: List[str] = []
    url: Optional[str] = f"https://ghcr.io/v2/{image_path}/tags/list?n=100"
    while url:
        try:
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                tags.extend(data.get("tags") or [])
                # Follow Link: <url>; rel="next" pagination header
                link_hdr = resp.headers.get("Link", "")
                url = None
                for part in link_hdr.split(","):
                    if 'rel="next"' in part:
                        m = re.search(r'<([^>]+)>', part)
                        if m:
                            url = m.group(1)
                        break
        except Exception:
            break
    return tags


def _check_manifest_exists(image_path: str, tag: str, token: str) -> bool:
    """Return True if image_path:tag has a manifest on GHCR (HEAD request)."""
    url = f"https://ghcr.io/v2/{image_path}/manifests/{tag}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.docker.distribution.manifest.v2+json",
    })
    req.get_method = lambda: "HEAD"
    try:
        with urllib.request.urlopen(req, timeout=5):
            return True
    except urllib.error.HTTPError as e:
        return e.code == 200
    except Exception:
        return False


def _get_github_release_tags(owner: str, repo: str) -> List[str]:
    """Return release tag_names from GitHub Releases API, newest first."""
    url = f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=10"
    try:
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "tt-model-runner/1.0",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            releases = json.loads(resp.read().decode())
            return [r["tag_name"] for r in releases if r.get("tag_name")]
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

    prefix_m = re.match(r'^v?([\d.]+)-', failed_tag)
    if prefix_m:
        prefix = prefix_m.group(1)
        same_prefix = [t for t in versioned if re.match(rf'^v?{re.escape(prefix)}-', t)]
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


def _resolve_via_github_releases(
    image_path: str,
    token: str,
    on_step,
) -> Optional[str]:
    """Try to find a working tag by checking GitHub Releases for the repo.

    Infers owner/repo from image_path (e.g. "tenstorrent/tt-inference-server/vllm-tt-metal"
    → owner="tenstorrent", repo="tt-inference-server").

    For each release tag (newest first), tries both the bare tag and a v-stripped/v-prefixed
    variant against the GHCR manifest API until one resolves.
    """
    parts = image_path.split("/")
    if len(parts) < 2:
        return None

    owner, repo = parts[0], parts[1]
    on_step(f"  → Querying GitHub Releases for {owner}/{repo}…")
    gh_tags = _get_github_release_tags(owner, repo)
    if not gh_tags:
        on_step("  ✗ No GitHub releases found")
        return None

    on_step(f"  → Found {len(gh_tags)} release(s); probing GHCR manifests…")
    for gh_tag in gh_tags:
        # Try the tag as-is, then with/without leading 'v'
        candidates = [gh_tag]
        if gh_tag.startswith("v"):
            candidates.append(gh_tag[1:])
        else:
            candidates.append(f"v{gh_tag}")

        for candidate in candidates:
            if _check_manifest_exists(image_path, candidate, token):
                on_step(f"  → Found via GitHub release: {candidate}")
                return candidate

    on_step("  ✗ No GitHub release tag found on GHCR")
    return None


def resolve_latest_tag(
    image_ref: str,
    on_step: Optional[callable] = None,
) -> Optional[str]:
    """Return a new image ref with the best available tag, or None on failure.

    Only handles ghcr.io. Returns None if resolution fails or the resolved tag
    is the same as the one that already failed.

    on_step(msg): optional callback called with progress strings during resolution.
    """
    def _step(msg: str):
        if on_step:
            on_step(msg)

    registry, image_path, failed_tag = parse_image_ref(image_ref)
    if not registry.endswith("ghcr.io"):
        _step("✗ Not a ghcr.io image — cannot auto-resolve")
        return None

    _step(f"  → Requesting GHCR pull token for {image_path}…")
    token = _get_ghcr_token(image_path)
    if not token:
        _step("  ✗ Could not obtain GHCR token (repo may be private or rate-limited)")
        return None

    # ── Strategy 1: GHCR tag listing ──────────────────────────────────────────
    _step("  → Listing available tags from GHCR…")
    tags = _list_tags(image_path, token)
    if tags:
        _step(f"  → Found {len(tags)} tag(s); selecting best match for '{failed_tag}'…")
        best = _best_tag(tags, failed_tag)
        if best:
            return f"{registry}/{image_path}:{best}"
        _step(f"  ✗ No tag found that improves on '{failed_tag}' in GHCR listing")
    else:
        _step("  ✗ GHCR tag listing returned no results (rate-limited or private)")

    # ── Strategy 2: GitHub Releases probe ─────────────────────────────────────
    best = _resolve_via_github_releases(image_path, token, _step)
    if best:
        return f"{registry}/{image_path}:{best}"

    # ── Strategy 3: :latest sentinel ──────────────────────────────────────────
    if failed_tag != "latest":
        _step("  → Trying :latest as last resort…")
        if _check_manifest_exists(image_path, "latest", token):
            _step("  → :latest exists on GHCR")
            return f"{registry}/{image_path}:latest"
        _step("  ✗ :latest not found either")

    return None
