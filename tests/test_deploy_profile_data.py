from deploy_profile import (
    ProfileSlot, DeployProfile, SlotRunState, OrchestratorCallbacks,
    DEVICE_CHIP_COUNT, ChipAssignmentError,
)
from launch_options import LaunchOptions


def test_profile_slot_defaults():
    slot = ProfileSlot(model_name="qwen", device_type="N150", port="8000")
    assert slot.chip_index is None
    assert slot.label == ""
    assert isinstance(slot.options, LaunchOptions)


def test_deploy_profile_defaults():
    p = DeployProfile(name="my-profile")
    assert p.slots == []
    assert p.description == ""


def test_device_chip_count_all_present():
    for dt in ("N150", "N300", "P100", "P150", "P300", "P300X2",
               "P150X4", "P150X8", "T3K", "BLACKHOLE_GALAXY"):
        assert dt in DEVICE_CHIP_COUNT


def test_chip_assignment_error_is_exception():
    assert isinstance(ChipAssignmentError("x"), Exception)
