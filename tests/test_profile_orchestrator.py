# tests/test_profile_orchestrator.py
import pytest
from dataclasses import dataclass
from typing import Optional
from unittest.mock import patch

from deploy_profile import (
    DeployProfile, ProfileSlot, OrchestratorCallbacks,
    ChipAssignmentError, DEVICE_CHIP_COUNT,
)
from profile_orchestrator import ProfileOrchestrator
from app_settings import AppSettings
from server_manager import ServerState


@dataclass
class _FakeChip:
    index: int
    board_type: str = "n150"
    temp_c: Optional[float] = None
    aiclk_mhz: Optional[int] = None
    fw_version: str = ""


def _orch(tmp_path):
    s = AppSettings(config_dir=str(tmp_path))
    s.server_repo_path = str(tmp_path)
    s.cache_root_path = str(tmp_path / "cache")
    cbs = OrchestratorCallbacks(
        on_slot_state=lambda *a: None,
        on_slot_log=lambda *a: None,
        on_slot_progress=lambda *a: None,
        on_profile_done=lambda: None,
    )
    return ProfileOrchestrator(settings=s, dispatch_fn=lambda fn, *a: fn(*a), callbacks=cbs)


def _slot(device_type="N150", chip_index=None):
    return ProfileSlot(model_name="qwen", device_type=device_type,
                       port="8000", chip_index=chip_index)


class TestAssignChips:
    def test_auto_single_chip(self, tmp_path):
        orch = _orch(tmp_path)
        chips = [_FakeChip(i) for i in range(4)]
        profile = DeployProfile(name="t", slots=[_slot("N150"), _slot("N150")])
        with patch("profile_orchestrator.get_chip_statuses_live", return_value=chips):
            result = orch._assign_chips(profile)
        assert result == [0, 1]

    def test_auto_two_chip_model(self, tmp_path):
        orch = _orch(tmp_path)
        chips = [_FakeChip(i) for i in range(4)]
        profile = DeployProfile(name="t", slots=[_slot("P300X2")])
        with patch("profile_orchestrator.get_chip_statuses_live", return_value=chips):
            result = orch._assign_chips(profile)
        assert result == [0]

    def test_pinned_chip_honored(self, tmp_path):
        orch = _orch(tmp_path)
        chips = [_FakeChip(i) for i in range(4)]
        profile = DeployProfile(name="t", slots=[_slot("N150", chip_index=3)])
        with patch("profile_orchestrator.get_chip_statuses_live", return_value=chips):
            result = orch._assign_chips(profile)
        assert result == [3]

    def test_mixed_pinned_and_auto(self, tmp_path):
        orch = _orch(tmp_path)
        chips = [_FakeChip(i) for i in range(4)]
        profile = DeployProfile(name="t", slots=[
            _slot("N150", chip_index=2),
            _slot("N150"),
        ])
        with patch("profile_orchestrator.get_chip_statuses_live", return_value=chips):
            result = orch._assign_chips(profile)
        assert result[0] == 2
        assert result[1] != 2

    def test_insufficient_chips_raises(self, tmp_path):
        orch = _orch(tmp_path)
        chips = [_FakeChip(0)]
        profile = DeployProfile(name="t", slots=[_slot("N150"), _slot("N150")])
        with patch("profile_orchestrator.get_chip_statuses_live", return_value=chips):
            with pytest.raises(ChipAssignmentError):
                orch._assign_chips(profile)

    def test_no_hardware_fallback(self, tmp_path):
        orch = _orch(tmp_path)
        profile = DeployProfile(name="t", slots=[_slot("N150")])
        with patch("profile_orchestrator.get_chip_statuses_live", return_value=[]):
            result = orch._assign_chips(profile)
        assert result == [0]

    def test_device_id_str_single(self, tmp_path):
        assert _orch(tmp_path)._device_id_str("N150", 2) == "2"

    def test_device_id_str_multi(self, tmp_path):
        assert _orch(tmp_path)._device_id_str("P300X2", 2) == "2,3"

    def test_device_id_str_four(self, tmp_path):
        assert _orch(tmp_path)._device_id_str("P150X4", 0) == "0,1,2,3"


def test_all_slots_ready_writes_last_success(tmp_path, monkeypatch):
    """ProfileOrchestrator writes a last-success entry when all slots are READY."""
    import sys; sys.path.insert(0, "app")
    import saved_config_store as scs
    monkeypatch.setattr(scs, "_SAVED_CONFIGS_DIR", tmp_path)

    from deploy_profile import DeployProfile, ProfileSlot, OrchestratorCallbacks
    from launch_options import LaunchOptions
    from profile_orchestrator import ProfileOrchestrator
    from server_manager import ServerState
    from unittest.mock import MagicMock, patch

    cbs = OrchestratorCallbacks(
        on_slot_state=lambda *a: None,
        on_slot_log=lambda *a: None,
        on_slot_progress=lambda *a: None,
        on_profile_done=lambda: None,
    )

    orch = ProfileOrchestrator(
        settings=MagicMock(server_repo_path="/tmp", cache_root_path="", hf_token=""),
        dispatch_fn=lambda fn, *a: fn(*a),
        callbacks=cbs,
    )

    profile = DeployProfile(
        name="my-profile",
        slots=[ProfileSlot(
            model_name="Llama-3.1-8B", device_type="n300",
            port="8001", options=LaunchOptions(),
        )],
        created="",
    )

    # Patch _run_sequential to simulate all-READY without real Docker
    from deploy_profile import SlotRunState
    from server_manager import ServerState as SS

    rs = SlotRunState(
        slot=profile.slots[0], state=SS.READY,
        server_mgr=MagicMock(), health_worker=MagicMock(),
        log_lines=[], port="8001", chip_index=0,
    )

    def fake_sequential(p, catalog, chips):
        orch._slot_states = [rs]
        # Simulate all-READY path: write last success then dispatch done
        orch._write_last_success_if_all_ready(p)
        orch._dispatch(orch._cbs.on_profile_done)

    orch._run_sequential = fake_sequential
    orch.launch_profile(profile, MagicMock())
    orch._launch_thread.join(timeout=5)

    loaded = scs.load_last_success()
    assert loaded is not None
    assert loaded.deploy_profile_name == "my-profile"
    assert loaded.model_name == ""
