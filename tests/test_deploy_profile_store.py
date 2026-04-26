# tests/test_deploy_profile_store.py
import pytest
from deploy_profile import DeployProfile, ProfileSlot
from launch_options import LaunchOptions


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    import deploy_profile_store as dps
    monkeypatch.setattr(dps, "_DEPLOY_PROFILES_DIR", tmp_path / "dp")
    profile = DeployProfile(
        name="test-profile", description="A test",
        slots=[ProfileSlot(model_name="qwen-7b", device_type="N150", port="8000", label="Chat")]
    )
    dps.save_deploy_profile(profile)
    loaded = dps.load_deploy_profile("test-profile")
    assert loaded is not None
    assert loaded.name == "test-profile"
    assert loaded.description == "A test"
    assert len(loaded.slots) == 1
    assert loaded.slots[0].model_name == "qwen-7b"
    assert loaded.slots[0].chip_index is None
    assert isinstance(loaded.slots[0].options, LaunchOptions)


def test_options_roundtrip(tmp_path, monkeypatch):
    import deploy_profile_store as dps
    monkeypatch.setattr(dps, "_DEPLOY_PROFILES_DIR", tmp_path / "dp")
    opts = LaunchOptions(max_model_len=32768, dev_mode=True)
    dps.save_deploy_profile(DeployProfile(
        name="opts-test",
        slots=[ProfileSlot(model_name="m", device_type="N150", port="8001", options=opts)]
    ))
    loaded = dps.load_deploy_profile("opts-test")
    assert loaded.slots[0].options.max_model_len == 32768
    assert loaded.slots[0].options.dev_mode is True


def test_list_profiles(tmp_path, monkeypatch):
    import deploy_profile_store as dps
    monkeypatch.setattr(dps, "_DEPLOY_PROFILES_DIR", tmp_path / "dp")
    for i in range(3):
        dps.save_deploy_profile(DeployProfile(name=f"profile-{i}", slots=[]))
    profiles = dps.list_deploy_profiles()
    assert len(profiles) == 3
    assert {p.name for p in profiles} == {"profile-0", "profile-1", "profile-2"}


def test_delete(tmp_path, monkeypatch):
    import deploy_profile_store as dps
    monkeypatch.setattr(dps, "_DEPLOY_PROFILES_DIR", tmp_path / "dp")
    dps.save_deploy_profile(DeployProfile(name="to-delete", slots=[]))
    assert dps.delete_deploy_profile("to-delete") is True
    assert dps.load_deploy_profile("to-delete") is None
    assert dps.delete_deploy_profile("to-delete") is False


def test_load_nonexistent_returns_none(tmp_path, monkeypatch):
    import deploy_profile_store as dps
    monkeypatch.setattr(dps, "_DEPLOY_PROFILES_DIR", tmp_path / "dp")
    assert dps.load_deploy_profile("nope") is None


def test_corrupt_file_skipped_in_list(tmp_path, monkeypatch):
    import deploy_profile_store as dps
    dp_dir = tmp_path / "dp"
    monkeypatch.setattr(dps, "_DEPLOY_PROFILES_DIR", dp_dir)
    dp_dir.mkdir(parents=True)
    (dp_dir / "corrupt.json").write_text("{not json")
    dps.save_deploy_profile(DeployProfile(name="good", slots=[]))
    profiles = dps.list_deploy_profiles()
    assert len(profiles) == 1 and profiles[0].name == "good"
