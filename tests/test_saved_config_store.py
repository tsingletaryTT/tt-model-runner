import json, dataclasses, pytest
from pathlib import Path

# ── Task 1 tests ──────────────────────────────────────────────────────────────

def test_saved_config_roundtrip_json():
    """SavedConfig serialises and deserialises through json without data loss."""
    import sys; sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
    from saved_config import SavedConfig
    cfg = SavedConfig(
        name="test",
        model_name="Llama-3.1-8B",
        device_type="n300",
        port=8000,
        docker_image="ghcr.io/tenstorrent/tt-inference-server:v0.0.1",
        inference_engine="vllm",
        options_json='{"use_case": "chat"}',
        deploy_profile_name="",
        created="2026-04-27T12:00:00",
        last_used="2026-04-27T12:05:00",
    )
    data = dataclasses.asdict(cfg)
    restored = SavedConfig(**data)
    assert restored == cfg

# ── Task 2 tests ──────────────────────────────────────────────────────────────

import saved_config_store as scs
from saved_config import SavedConfig


def _make_cfg(name="test", profile="") -> SavedConfig:
    return SavedConfig(
        name=name, model_name="Llama", device_type="n300", port=8000,
        docker_image="ghcr.io/tt/img:v1", inference_engine="vllm",
        options_json='{"use_case":"chat"}',
        deploy_profile_name=profile, created="", last_used="",
    )


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(scs, "_SAVED_CONFIGS_DIR", tmp_path)
    return tmp_path


def test_save_and_load_last_success():
    cfg = _make_cfg("__last_success__")
    scs.save_last_success(cfg)
    loaded = scs.load_last_success()
    assert loaded is not None
    assert loaded.name == "__last_success__"
    assert loaded.model_name == "Llama"


def test_load_last_success_returns_none_when_absent():
    assert scs.load_last_success() is None


def test_save_named_and_list():
    scs.save_named(_make_cfg("my-config"))
    scs.save_named(_make_cfg("another-config"))
    names = scs.list_named()
    assert names == ["another-config", "my-config"]


def test_list_named_excludes_last_success():
    scs.save_last_success(_make_cfg("__last_success__"))
    scs.save_named(_make_cfg("named"))
    assert scs.list_named() == ["named"]


def test_load_named_roundtrip():
    scs.save_named(_make_cfg("roundtrip"))
    loaded = scs.load_named("roundtrip")
    assert loaded.device_type == "n300"
    assert loaded.name == "roundtrip"


def test_delete_named():
    scs.save_named(_make_cfg("todelete"))
    scs.delete_named("todelete")
    assert "todelete" not in scs.list_named()


def test_delete_last_success_raises():
    with pytest.raises(ValueError, match="__last_success__"):
        scs.delete_named("__last_success__")


def test_save_named_rejects_sentinel():
    with pytest.raises(ValueError, match="__last_success__"):
        scs.save_named(_make_cfg("__last_success__"))


def test_path_traversal_guard():
    with pytest.raises(ValueError, match="Invalid"):
        scs.save_named(_make_cfg("../evil"))


def test_created_timestamp_set_on_first_save():
    cfg = _make_cfg("ts-test")
    assert cfg.created == ""
    scs.save_named(cfg)
    loaded = scs.load_named("ts-test")
    assert loaded.created != ""


def test_load_named_raises_for_absent():
    with pytest.raises(FileNotFoundError):
        scs.load_named("nonexistent")


def test_atomic_write(tmp_path):
    """File is written via tmp+rename so a partial write is never observed."""
    cfg = _make_cfg("atomic")
    scs.save_named(cfg)
    assert (tmp_path / "atomic.json").exists()
    assert not list(tmp_path.glob("*.tmp"))
