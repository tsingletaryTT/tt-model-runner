#!/usr/bin/env python3
"""Tests for ghcr_resolver — pure-logic only, no network calls."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from ghcr_resolver import parse_image_ref, _best_tag


def test_parse_simple():
    r, p, t = parse_image_ref("ghcr.io/tenstorrent/tt-media-inference-server:0.12.0-2508216")
    assert r == "ghcr.io"
    assert p == "tenstorrent/tt-media-inference-server"
    assert t == "0.12.0-2508216"


def test_parse_nested_path():
    r, p, t = parse_image_ref(
        "ghcr.io/tenstorrent/tt-inference-server/vllm-tt-metal-src-release-v0.56.0:v0.56.0"
    )
    assert r == "ghcr.io"
    assert p == "tenstorrent/tt-inference-server/vllm-tt-metal-src-release-v0.56.0"
    assert t == "v0.56.0"


def test_parse_no_tag():
    r, p, t = parse_image_ref("ghcr.io/tenstorrent/some-image")
    assert r == "ghcr.io"
    assert p == "tenstorrent/some-image"
    assert t == ""


def test_best_tag_same_prefix():
    tags = ["0.12.0-2508210", "0.12.0-2508216", "0.12.0-2508220", "0.11.0-2507001", "latest"]
    # Failed on 2508216 — should pick the newest in same prefix
    result = _best_tag(tags, "0.12.0-2508216")
    assert result == "0.12.0-2508220"


def test_best_tag_no_prefix_match():
    tags = ["0.13.0-2509001", "0.14.0-2510001", "latest"]
    result = _best_tag(tags, "0.12.0-2508216")
    # Falls back to highest numeric tag
    assert result == "0.14.0-2510001"


def test_best_tag_returns_none_if_same():
    # Only available tag is the one that already failed
    tags = ["0.12.0-2508216"]
    result = _best_tag(tags, "0.12.0-2508216")
    assert result is None


def test_best_tag_empty():
    assert _best_tag([], "0.12.0-2508216") is None


def test_best_tag_only_latest():
    tags = ["latest"]
    result = _best_tag(tags, "0.12.0-2508216")
    assert result == "latest"
