# tests/test_profiles.py
import json
import sys
from pathlib import Path
import tempfile
import pytest
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from launch_options import LaunchOptions
import profiles as prof_module


@pytest.fixture(autouse=True)
def tmp_profiles_dir(tmp_path, monkeypatch):
    """Redirect all profile I/O to a temp directory."""
    monkeypatch.setattr(prof_module, "_PROFILES_DIR", tmp_path / "profiles")
    return tmp_path / "profiles"


def test_save_and_load_roundtrip():
    opts = LaunchOptions(use_case="dev", dev_mode=True, max_model_len=32768)
    prof_module.save_profile("my-profile", "test profile", "LLM", opts)
    loaded = prof_module.load_profile("my-profile")
    assert loaded is not None
    assert loaded["name"] == "my-profile"
    assert loaded["options"]["dev_mode"] is True
    assert loaded["options"]["max_model_len"] == 32768


def test_load_nonexistent_returns_none():
    assert prof_module.load_profile("does-not-exist") is None


def test_list_profiles_empty():
    assert prof_module.list_profiles() == []


def test_list_profiles_returns_saved():
    prof_module.save_profile("p1", "", "LLM", LaunchOptions())
    prof_module.save_profile("p2", "", "IMAGE", LaunchOptions())
    result = prof_module.list_profiles()
    assert len(result) == 2
    names = {p["name"] for p in result}
    assert names == {"p1", "p2"}


def test_list_profiles_filter_by_model_type():
    prof_module.save_profile("llm-prof", "", "LLM", LaunchOptions())
    prof_module.save_profile("img-prof", "", "IMAGE", LaunchOptions())
    result = prof_module.list_profiles(model_type="LLM")
    assert len(result) == 1
    assert result[0]["name"] == "llm-prof"


def test_delete_profile():
    prof_module.save_profile("to-delete", "", "LLM", LaunchOptions())
    assert prof_module.delete_profile("to-delete") is True
    assert prof_module.load_profile("to-delete") is None


def test_delete_nonexistent_returns_false():
    assert prof_module.delete_profile("ghost") is False


def test_corrupt_profile_skipped_in_list(tmp_profiles_dir):
    tmp_profiles_dir.mkdir(parents=True, exist_ok=True)
    (tmp_profiles_dir / "bad.json").write_text("not json")
    prof_module.save_profile("good", "", "LLM", LaunchOptions())
    result = prof_module.list_profiles()
    # corrupt file is silently skipped
    assert len(result) == 1
    assert result[0]["name"] == "good"


def test_overwrite_existing_profile():
    prof_module.save_profile("p", "", "LLM", LaunchOptions(dev_mode=False))
    prof_module.save_profile("p", "", "LLM", LaunchOptions(dev_mode=True))
    loaded = prof_module.load_profile("p")
    assert loaded["options"]["dev_mode"] is True


def test_profile_to_options_roundtrip():
    opts = LaunchOptions(use_case="dev", dev_mode=True, max_model_len=32768)
    prof_module.save_profile("rt", "", "LLM", opts)
    loaded = prof_module.load_profile("rt")
    assert prof_module.profile_to_options(loaded) == opts


def test_profile_to_options_ignores_unknown_keys():
    profile = {"name": "x", "options": {"dev_mode": True, "future_key": "ignored"}}
    opts = prof_module.profile_to_options(profile)
    assert opts.dev_mode is True
