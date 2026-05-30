#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Data model for deploy profiles — named multi-slot model-to-chip assignments."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, List, Optional

from launch_options import LaunchOptions

if TYPE_CHECKING:
    from health_worker import HealthWorker
    from server_manager import ServerManager, ServerState


class ChipAssignmentError(Exception):
    """Raised when chip assignment cannot satisfy the profile's chip requirements."""


# Maps device_type → number of physical chips consumed
DEVICE_CHIP_COUNT: dict = {
    "N150":             1,
    "N300":             2,
    "P100":             1,
    "P150":             1,
    "P300":             2,
    "P300X2":           2,
    "P150X4":           4,
    "N150X4":           4,    # 4× N150 chips
    "P150X8":           8,
    "T3K":              8,
    "BLACKHOLE_GALAXY": 8,
    "GALAXY":           8,    # 1 Blackhole Galaxy = 8 chips
    "GALAXY_T3K":       16,   # 1 Galaxy (8 BH) + T3K (8 WH) = 16 chips, mixed arch
    "DUAL_GALAXY":      16,   # 2 Galaxy = 16 chips
    "QUAD_GALAXY":      32,   # 4 Galaxy = 32 chips
}


@dataclass
class ProfileSlot:
    """One model-to-chip assignment within a deploy profile."""
    model_name: str          # ModelCatalog key, e.g. "qwen2_5-7b-instruct"
    device_type: str         # e.g. "N150", "P300X2"
    port: str                # e.g. "8000"
    chip_index: Optional[int] = None   # None = auto-assign; int = pinned
    options: LaunchOptions = field(default_factory=LaunchOptions)
    label: str = ""


@dataclass
class DeployProfile:
    """Named, saveable collection of ProfileSlots."""
    name: str
    description: str = ""
    created: str = ""
    slots: List[ProfileSlot] = field(default_factory=list)


@dataclass
class SlotRunState:
    """Runtime-only state for one running slot (never persisted)."""
    slot: ProfileSlot
    state: ServerState
    server_mgr: ServerManager
    health_worker: Optional[HealthWorker]
    log_lines: List[str]     # capped at 200
    port: str
    chip_index: int          # resolved (never None at runtime)


@dataclass
class OrchestratorCallbacks:
    """Callbacks from ProfileOrchestrator to the GTK view."""
    on_slot_state: Callable[[int, ServerState, str], None]
    on_slot_log: Callable[[int, str], None]
    on_slot_progress: Callable[[int, float, str], None]
    on_profile_done: Callable[[], None]
