# tests/test_docker_images.py
import sys
from pathlib import Path
import subprocess
import pytest
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from docker_images import scan_local_images, DockerImage

_FAKE_OUTPUT = """\
ghcr.io/tenstorrent/tt-inference-server/vllm-tt-metal:v0.56.0\t4.21 GB\t3 days ago
ghcr.io/tenstorrent/tt-media-inference-server:0.12.0-2508220\t2.10 GB\t5 hours ago
ubuntu:22.04\t77.8 MB\t2 weeks ago
"""


def _mock_run(cmd, **kwargs):
    class R:
        returncode = 0
        stdout = _FAKE_OUTPUT
        stderr = ""
    return R()


def test_filters_tt_images_only(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _mock_run)
    images = scan_local_images()
    assert len(images) == 2
    assert all(img.is_tt for img in images)
    repos = {img.repo_tag for img in images}
    assert "ubuntu:22.04" not in str(repos)


def test_fields_populated(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _mock_run)
    images = scan_local_images()
    media = next(i for i in images if "tt-media" in i.repo_tag)
    assert media.size_str == "2.10 GB"
    assert media.created_str == "5 hours ago"


def test_spec_default_not_pulled_prepended(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _mock_run)
    spec_image = "ghcr.io/tenstorrent/tt-media-inference-server:0.12.0-NEW"
    images = scan_local_images(spec_default=spec_image)
    assert images[0].repo_tag == spec_image
    assert images[0].size_str == "—"
    assert images[0].created_str == "not pulled"


def test_spec_default_found_moved_to_front(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _mock_run)
    spec_image = "ghcr.io/tenstorrent/tt-media-inference-server:0.12.0-2508220"
    images = scan_local_images(spec_default=spec_image)
    assert images[0].repo_tag == spec_image
    assert images[0].size_str == "2.10 GB"


def test_docker_not_available(monkeypatch):
    def _raise(*a, **kw):
        raise FileNotFoundError("docker not found")
    monkeypatch.setattr(subprocess, "run", _raise)
    assert scan_local_images() == []


def test_docker_error_returns_empty(monkeypatch):
    class R:
        returncode = 1
        stdout = ""
        stderr = "permission denied"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: R())
    assert scan_local_images() == []


def test_short_tag_property():
    img = DockerImage(
        repo_tag="ghcr.io/tenstorrent/vllm-tt-metal:v0.56.0",
        size_str="4 GB",
        created_str="1 day ago",
        is_tt=True,
    )
    assert img.short_tag == "vllm-tt-metal:v0.56.0"
