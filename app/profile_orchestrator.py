#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""ProfileOrchestrator — sequentially launch a DeployProfile's slots.

Responsibilities
----------------
1. Chip assignment: resolve which physical chip index each slot uses,
   respecting pinned slots and honouring multi-chip device types
   (e.g. P300X2 uses 2 consecutive chips).
2. Sequential launch: start each slot's ServerManager + HealthWorker in order,
   waiting for READY (or ERROR) before proceeding to the next slot.
3. Callback dispatch: all state/log updates are posted through the caller's
   dispatch_fn so GTK / TUI event loops are never touched from this thread.
"""
import threading
from typing import Callable, List, Optional

from deploy_profile import (
    ChipAssignmentError, DEVICE_CHIP_COUNT,
    DeployProfile, OrchestratorCallbacks, SlotRunState,
)
from device_detector import get_chip_statuses_live
from health_worker import HealthWorker
from launch_config_builder import build_launch_config
from model_catalog import ModelCatalog
from server_manager import ServerManager, ServerState


class ProfileOrchestrator:
    """Manages sequential multi-slot deployment for a DeployProfile.

    Usage
    -----
    orch = ProfileOrchestrator(settings, dispatch_fn, callbacks)
    orch.launch_profile(profile, catalog)   # returns immediately; runs in bg
    orch.stop_all()                         # graceful shutdown

    All OrchestratorCallbacks are invoked through dispatch_fn, so it is safe
    to update GTK or Textual widgets inside them.
    """

    def __init__(
        self,
        settings,
        dispatch_fn: Callable,
        callbacks: OrchestratorCallbacks,
    ):
        """Initialise orchestrator.

        Parameters
        ----------
        settings:
            AppSettings instance with server_repo_path, cache_root_path, etc.
        dispatch_fn:
            Scheduler that posts ``fn(*args)`` onto the UI event loop.
            Pass ``GLib.idle_add`` for GTK, ``app.call_from_thread`` for TUI,
            or ``lambda fn, *a: fn(*a)`` for synchronous tests.
        callbacks:
            OrchestratorCallbacks dataclass with per-slot and profile hooks.
        """
        self._settings = settings
        self._dispatch = dispatch_fn
        self._cbs = callbacks
        # One SlotRunState per slot; None until that slot has started launching.
        self._slot_states: List[Optional[SlotRunState]] = []
        self._launch_thread: Optional[threading.Thread] = None
        self._stop_requested = False

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def slot_states(self) -> List[Optional[SlotRunState]]:
        """Snapshot of current per-slot run state (defensive copy)."""
        return list(self._slot_states)

    @property
    def is_running(self) -> bool:
        """True while any slot is in a transient (non-terminal) state."""
        return any(
            s is not None and s.state not in (
                ServerState.IDLE, ServerState.ERROR, ServerState.DONE, ServerState.READY
            )
            for s in self._slot_states
        )

    def launch_profile(self, profile: DeployProfile, catalog: ModelCatalog) -> None:
        """Launch all slots sequentially in a background daemon thread.

        If a previous launch is still in progress this call is ignored.
        Chip assignment errors are reported synchronously through callbacks
        before the thread is started.
        """
        if self._launch_thread and self._launch_thread.is_alive():
            return

        self._stop_requested = False
        self._slot_states = [None] * len(profile.slots)

        # Chip assignment is quick and synchronous; report errors immediately.
        try:
            resolved_chips = self._assign_chips(profile)
        except ChipAssignmentError as e:
            for idx in range(len(profile.slots)):
                self._dispatch(self._cbs.on_slot_state, idx, ServerState.ERROR, str(e))
            self._dispatch(self._cbs.on_profile_done)
            return

        self._launch_thread = threading.Thread(
            target=self._run_sequential,
            args=(profile, catalog, resolved_chips),
            daemon=True,
            name="profile-launch",
        )
        self._launch_thread.start()

    def stop_slot(self, slot_idx: int) -> None:
        """Stop a single slot by index (no-op if out of range or not started)."""
        if slot_idx >= len(self._slot_states):
            return
        rs = self._slot_states[slot_idx]
        if rs is None:
            return
        if rs.health_worker:
            rs.health_worker.stop()
        rs.server_mgr.stop()
        rs.state = ServerState.STOPPING
        self._dispatch(self._cbs.on_slot_state, slot_idx, ServerState.STOPPING, "")

    def stop_all(self) -> None:
        """Signal the launch loop to halt and stop every running slot."""
        self._stop_requested = True
        for idx in range(len(self._slot_states)):
            self.stop_slot(idx)

    # ── Chip assignment ───────────────────────────────────────────────────────

    def _assign_chips(self, profile: DeployProfile) -> List[int]:
        """Return the resolved start-chip index for every slot.

        Algorithm
        ---------
        1. Query live chip list via ``get_chip_statuses_live()``.  If that
           returns an empty list (no Tenstorrent hardware / tt-smi unavailable),
           fall back to a synthetic sequential index range sized to fit all slots.
        2. Pre-mark every index that a *pinned* slot (``slot.chip_index is not None``)
           consumes, including the N-1 additional chips needed by multi-chip devices.
        3. Walk the slots in order; pinned slots contribute their fixed index;
           auto slots pull the required number of chips from the front of the
           remaining free list.
        4. If the free list is exhausted before all auto slots are satisfied,
           raise ``ChipAssignmentError``.

        Returns
        -------
        List[int] of length ``len(profile.slots)``, where each value is the
        *first* chip index assigned to that slot (the device_id string includes
        any additional consecutive chips via ``_device_id_str``).
        """
        chips = get_chip_statuses_live()

        if chips:
            # Use the indices reported by the hardware.
            all_indices = sorted(c.index for c in chips)
        else:
            # No hardware detected — synthesise a pool just large enough.
            total_needed = sum(
                DEVICE_CHIP_COUNT.get(s.device_type, 1) for s in profile.slots
            )
            all_indices = list(range(total_needed))

        # Mark every index pre-consumed by pinned slots (including span chips).
        pinned_used: set = set()
        for slot in profile.slots:
            if slot.chip_index is not None:
                span = DEVICE_CHIP_COUNT.get(slot.device_type, 1)
                for offset in range(span):
                    pinned_used.add(slot.chip_index + offset)

        # Build the free pool: indices not already reserved by pinned slots.
        free = [idx for idx in all_indices if idx not in pinned_used]

        result: List[int] = []
        for slot in profile.slots:
            if slot.chip_index is not None:
                # Pinned — use exactly what the slot specifies.
                result.append(slot.chip_index)
            else:
                span = DEVICE_CHIP_COUNT.get(slot.device_type, 1)
                if len(free) < span:
                    label = slot.label or slot.model_name
                    raise ChipAssignmentError(
                        f"Slot '{label}' needs {span} chip(s) ({slot.device_type}) "
                        f"but only {len(free)} free chip(s) remain"
                    )
                result.append(free[0])
                free = free[span:]  # consume the required span

        return result

    def _device_id_str(self, device_type: str, start_chip: int) -> str:
        """Format the DEVICE_ID env-var value for a slot.

        For single-chip devices (span=1) this is just the chip index as a
        string.  For multi-chip devices the span-many consecutive indices are
        comma-joined.

        Examples
        --------
        >>> _device_id_str("N150", 2)   → "2"
        >>> _device_id_str("P300X2", 2) → "2,3"
        >>> _device_id_str("P150X4", 0) → "0,1,2,3"
        """
        span = DEVICE_CHIP_COUNT.get(device_type, 1)
        return ",".join(str(start_chip + offset) for offset in range(span))

    # ── Post-launch persistence ───────────────────────────────────────────────

    def _write_last_success_if_all_ready(self, profile) -> None:
        """Write a last-success SavedConfig if every slot reached READY.

        Called at the end of _run_sequential.  If even one slot is not READY
        (e.g. ERROR, STOPPING, or still None), the write is skipped so we only
        record genuinely complete deployments.

        The SavedConfig is a profile-only record: single-server fields
        (model_name, device_type, port, docker_image, inference_engine) are left
        empty / 0, and deploy_profile_name carries the profile's name so the
        restore path knows which DeployProfile to reload.

        Any I/O failure is silently swallowed — the orchestrator must never
        crash because of a failed metrics write.
        """
        all_ready = (
            bool(self._slot_states)
            and all(
                s is not None and s.state == ServerState.READY
                for s in self._slot_states
            )
        )
        if not all_ready:
            return
        try:
            from saved_config import SavedConfig
            from saved_config_store import save_last_success
            save_last_success(SavedConfig(
                name="__last_success__",
                model_name="",
                device_type="",
                port=0,
                docker_image="",
                inference_engine="",
                options_json="{}",
                deploy_profile_name=profile.name,
                created="",
                last_used="",
            ))
        except Exception:
            # Never crash the orchestrator over a save failure.
            pass

    # ── Sequential launch loop ────────────────────────────────────────────────

    def _run_sequential(
        self,
        profile: DeployProfile,
        catalog: ModelCatalog,
        resolved_chips: List[int],
    ) -> None:
        """Background thread: launch each slot in order, waiting for READY.

        On READY the next slot starts.  On ERROR or stop-request the loop
        breaks and ``on_profile_done`` is dispatched.
        """
        for idx, (slot, chip_idx) in enumerate(zip(profile.slots, resolved_chips)):
            if self._stop_requested:
                break

            # Resolve model entry from catalog.
            entry = catalog.get_entry(slot.model_name, slot.device_type)
            if entry is None:
                msg = (
                    f"Model '{slot.model_name}' / {slot.device_type} "
                    f"not found in catalog"
                )
                rs = SlotRunState(
                    slot=slot,
                    state=ServerState.ERROR,
                    server_mgr=ServerManager(),
                    health_worker=None,
                    log_lines=[msg],
                    port=slot.port,
                    chip_index=chip_idx,
                )
                self._slot_states[idx] = rs
                self._dispatch(self._cbs.on_slot_state, idx, ServerState.ERROR, msg)
                self.stop_all()  # shut down any slots that were already started
                break

            device_id = self._device_id_str(slot.device_type, chip_idx)
            # Event set when this slot reaches READY or ERROR.
            ready_event = threading.Event()

            # ── Closure-based callbacks for this slot ─────────────────────
            # ``i`` and ``ev`` are captured by value via default arguments to
            # avoid the classic late-binding closure bug.

            def _log_cb(line: str, i: int = idx) -> None:
                """Append a log line to the slot's in-memory buffer and dispatch."""
                rs = self._slot_states[i]
                if rs is None:
                    return
                rs.log_lines.append(line)
                # Keep memory bounded to the 200 most-recent lines.
                if len(rs.log_lines) > 200:
                    rs.log_lines.pop(0)
                self._dispatch(self._cbs.on_slot_log, i, line)

            def _state_cb(state: ServerState, i: int = idx) -> None:
                """Propagate ServerManager state changes; unblock on ERROR."""
                rs = self._slot_states[i]
                if rs is None:
                    return
                rs.state = state
                self._dispatch(self._cbs.on_slot_state, i, state, "")
                if state == ServerState.ERROR:
                    # Unblock the ready_event so the loop can exit cleanly.
                    ready_event.set()

            def _on_ready(models, i: int = idx, ev: threading.Event = ready_event) -> None:
                """Health probe reported the server is ready."""
                rs = self._slot_states[i]
                if rs is None:
                    return
                rs.state = ServerState.READY
                self._dispatch(self._cbs.on_slot_state, i, ServerState.READY, "")
                ev.set()

            # ── Build managers ─────────────────────────────────────────────
            mgr = ServerManager()
            # Map inference_engine to the two health-probe modes supported by
            # HealthWorker: "media" or "vllm" (the default).
            _engine = "media" if entry.inference_engine == "media" else "vllm"
            hw = HealthWorker(
                port=slot.port,
                on_ready=_on_ready,
                # on_lost is intentionally a no-op: ProfileOrchestrator only manages
                # sequential startup; post-READY health monitoring is out of scope.
                on_lost=lambda: None,
                dispatch_fn=self._dispatch,
                engine=_engine,
            )

            # ── Register slot state ────────────────────────────────────────
            rs = SlotRunState(
                slot=slot,
                state=ServerState.LAUNCHING,
                server_mgr=mgr,
                health_worker=hw,
                log_lines=[],
                port=slot.port,
                chip_index=chip_idx,
            )
            self._slot_states[idx] = rs
            self._dispatch(self._cbs.on_slot_state, idx, ServerState.LAUNCHING, "")

            # ── Build launch config and start ──────────────────────────────
            config = build_launch_config(
                model_name=entry.display_name,
                device_type=slot.device_type,
                port=slot.port,
                device_id=device_id,
                inference_engine=entry.inference_engine,
                settings=self._settings,
                options=slot.options,
                log_cb=_log_cb,
            )

            hw.start()
            mgr.launch(config, on_log_line=_log_cb, on_state=_state_cb)

            # Block until READY or ERROR (30-minute guard against hung servers).
            ready_event.wait(timeout=1800)

            if self._stop_requested or rs.state == ServerState.ERROR:
                # Stop any slots that already reached READY before failing.
                self.stop_all()
                break

        # Notify the UI that the full profile launch sequence has completed
        # (either all slots READY, an error halted the run, or stop was requested).
        self._write_last_success_if_all_ready(profile)
        self._dispatch(self._cbs.on_profile_done)
