#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""GTK Deploy Panel — create, edit, and launch deploy profiles."""
from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, Pango

from typing import Callable, List, Optional

from deploy_profile import (
    DEVICE_CHIP_COUNT, DeployProfile, OrchestratorCallbacks,
    ProfileSlot,
)
from deploy_profile_store import (
    list_deploy_profiles,
    load_deploy_profile, save_deploy_profile,
)
from model_catalog import ModelCatalog
from profile_orchestrator import ProfileOrchestrator
from server_manager import ServerState

# Only show models that fit on 1 or 2 chips in the slot editor
_ELIGIBLE_DEVICE_TYPES = {dt for dt, n in DEVICE_CHIP_COUNT.items() if n <= 2}

# Maps each ServerState to the display text and CSS class for the state badge pill
_STATE_CSS = {
    ServerState.LAUNCHING:     ("LAUNCHING",     "pill-loading"),
    ServerState.PULLING_IMAGE: ("PULLING IMAGE", "pill-loading"),
    ServerState.LOADING:       ("LOADING",       "pill-loading"),
    ServerState.READY:         ("READY",         "pill-ready"),
    ServerState.ERROR:         ("ERROR",         "pill-error"),
    ServerState.STOPPING:      ("STOPPING",      "pill-stopping"),
    ServerState.RUNNING:       ("RUNNING",       "pill-loading"),
    ServerState.IDLE:          ("IDLE",          "pill-idle"),
    ServerState.DONE:          ("DONE",          "pill-ready"),
}


class _SlotWidgets:
    """Widget references for one slot editor row.

    Holds references to all interactive widgets for a single ProfileSlot
    row in the editing page, and provides a ``to_slot()`` method to
    serialize the current widget state back to a :class:`ProfileSlot`.
    """

    def __init__(self, row, model_dd, device_lbl, port_entry,
                 chip_entry, label_entry, model_keys):
        self.row = row
        self.model_dd = model_dd
        self.device_lbl = device_lbl
        self.port_entry = port_entry
        self.chip_entry = chip_entry
        self.label_entry = label_entry
        # Parallel list to model_dd strings: [(model_name, device_type), ...]
        self.model_keys = model_keys

    def to_slot(self) -> Optional[ProfileSlot]:
        """Return a ProfileSlot from current widget state, or None if no model is selected."""
        sel = self.model_dd.get_selected()
        if sel == Gtk.INVALID_LIST_POSITION or sel >= len(self.model_keys):
            return None
        model_name, device_type = self.model_keys[sel]
        port = self.port_entry.get_text().strip() or "8000"
        chip_text = self.chip_entry.get_text().strip()
        # chip_index is None (auto-assign) unless the user typed a valid integer
        chip_index = int(chip_text) if chip_text.isdigit() else None
        label = self.label_entry.get_text().strip()
        return ProfileSlot(
            model_name=model_name,
            device_type=device_type,
            port=port,
            chip_index=chip_index,
            label=label,
        )


class DeployPanel(Gtk.Box):
    """Deploy Profiles panel: left profile list + right editor/running area.

    Layout
    ------
    * Left sidebar (220 px) — scrollable list of saved profiles + "New" button
    * Vertical separator
    * Right area — a :class:`Gtk.Stack` with three named pages:

      - ``"empty"``   — shown on startup before any profile is selected
      - ``"editing"`` — form to edit name, description, and slot rows
      - ``"running"`` — live per-slot status cards while a profile is running

    The panel wires itself to ``orchestrator`` by setting
    ``orchestrator._cbs`` (OrchestratorCallbacks).  All callbacks arrive on
    the GTK main thread because the caller must have injected
    ``GLib.idle_add`` as the orchestrator's ``dispatch_fn``.
    """

    def __init__(
        self,
        orchestrator: ProfileOrchestrator,
        get_catalog: Callable[[], Optional[ModelCatalog]],
    ):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._orch = orchestrator
        self._get_catalog = get_catalog
        # The profile currently loaded in the editing page (may be unsaved)
        self._current_profile: Optional[DeployProfile] = None
        # Widget bookkeeping for the slot editor rows
        self._slot_widgets: List[_SlotWidgets] = []
        # Per-slot status cards shown in the running page
        self._slot_cards: List[_SlotCard] = []

        # Wire orchestrator → this panel.  All callbacks are dispatched
        # through the orchestrator's dispatch_fn so they arrive on the GTK
        # main thread.
        self._orch._cbs = OrchestratorCallbacks(
            on_slot_state=self._on_slot_state,
            on_slot_log=self._on_slot_log,
            on_slot_progress=lambda i, f, l: None,  # progress bar not shown yet
            on_profile_done=self._on_profile_done,
        )

        self._build()

    # ── Widget construction ──────────────────────────────────────────────────

    def _build(self):
        """Assemble sidebar | separator | right-stack."""
        self.append(self._build_sidebar())
        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        self._right_stack = Gtk.Stack()
        self._right_stack.set_hexpand(True)
        self._right_stack.set_vexpand(True)
        self._right_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._right_stack.add_named(self._build_empty_page(), "empty")
        self._right_stack.add_named(self._build_editing_page(), "editing")
        self._right_stack.add_named(self._build_running_page(), "running")
        self._right_stack.set_visible_child_name("empty")
        self.append(self._right_stack)

    def _build_sidebar(self) -> Gtk.Box:
        """Left-hand profile list with a New Profile button at the bottom."""
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        sidebar.set_size_request(220, -1)

        hdr = Gtk.Label(label="DEPLOY PROFILES")
        hdr.add_css_class("section-label")
        hdr.set_margin_top(12)
        hdr.set_margin_bottom(8)
        hdr.set_margin_start(12)
        hdr.set_xalign(0)
        sidebar.append(hdr)

        # Scrollable list box populated by _refresh_profile_list()
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        self._profile_list = Gtk.ListBox()
        self._profile_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._profile_list.connect("row-selected", self._on_profile_row_selected)
        scroll.set_child(self._profile_list)
        sidebar.append(scroll)

        new_btn = Gtk.Button(label="＋  New Profile")
        new_btn.set_margin_top(8)
        new_btn.set_margin_bottom(8)
        new_btn.set_margin_start(8)
        new_btn.set_margin_end(8)
        new_btn.connect("clicked", lambda _: self._on_new_profile())
        sidebar.append(new_btn)

        self._refresh_profile_list()
        return sidebar

    def _build_empty_page(self) -> Gtk.Box:
        """Placeholder shown before any profile is selected or created."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        lbl = Gtk.Label(label="Select or create a deploy profile")
        lbl.add_css_class("muted")
        box.append(lbl)
        return box

    def _build_editing_page(self) -> Gtk.Box:
        """Form for editing a profile's name, description, and slot list."""
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(16)
        page.set_margin_start(20)
        page.set_margin_end(20)
        page.set_margin_bottom(16)

        # Name and description text fields
        for field_label, attr in [("Name:", "_name_entry"), ("Description:", "_desc_entry")]:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            lbl = Gtk.Label(label=field_label)
            lbl.set_xalign(1)
            lbl.set_width_chars(12)
            entry = Gtk.Entry()
            entry.set_hexpand(True)
            setattr(self, attr, entry)
            row.append(lbl)
            row.append(entry)
            page.append(row)

        page.append(Gtk.Separator())

        # Column header row for the slot editor
        hdr_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hdr_row.set_margin_start(4)
        for txt, chars, expand in [
            ("Model", 0, True), ("Device", 8, False),
            ("Port", 6, False), ("Chip", 5, False), ("Label", 8, False),
        ]:
            lbl = Gtk.Label(label=txt)
            lbl.add_css_class("muted")
            lbl.set_hexpand(expand)
            if chars:
                lbl.set_width_chars(chars)
            hdr_row.append(lbl)
        page.append(hdr_row)

        # Scrollable container holding one row per slot
        slot_scroll = Gtk.ScrolledWindow()
        slot_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        slot_scroll.set_vexpand(True)
        self._slots_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        slot_scroll.set_child(self._slots_box)
        page.append(slot_scroll)

        add_btn = Gtk.Button(label="＋  Add Slot")
        add_btn.set_halign(Gtk.Align.START)
        add_btn.connect("clicked", lambda _: self._add_slot_row())
        page.append(add_btn)

        # Save / Launch buttons aligned to the right
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.END)
        self._save_btn = Gtk.Button(label="Save Profile")
        self._save_btn.connect("clicked", lambda _: self._on_save())
        self._launch_edit_btn = Gtk.Button(label="Launch Profile")
        self._launch_edit_btn.add_css_class("launch-btn")
        self._launch_edit_btn.connect("clicked", lambda _: self._on_launch_from_edit())
        btn_row.append(self._save_btn)
        btn_row.append(self._launch_edit_btn)
        page.append(btn_row)

        return page

    def _build_running_page(self) -> Gtk.Box:
        """Live view of a running profile — one _SlotCard per slot."""
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_margin_top(16)
        page.set_margin_start(20)
        page.set_margin_end(20)
        page.set_margin_bottom(16)

        # Header updated to show the active profile name
        self._running_header = Gtk.Label(label="Running profile")
        self._running_header.add_css_class("section-label")
        self._running_header.set_xalign(0)
        page.append(self._running_header)

        # Scrollable list of _SlotCard widgets
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        self._cards_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        scroll.set_child(self._cards_box)
        page.append(scroll)

        # Action row: go back to editing or stop all slots
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.END)
        stop_all = Gtk.Button(label="Stop All")
        stop_all.add_css_class("stop-btn")
        stop_all.connect("clicked", lambda _: self._orch.stop_all())
        back_btn = Gtk.Button(label="← Back to Editor")
        back_btn.add_css_class("flat")
        back_btn.connect("clicked", lambda _: self._right_stack.set_visible_child_name("editing"))
        btn_row.append(back_btn)
        btn_row.append(stop_all)
        page.append(btn_row)

        return page

    # ── Sidebar helpers ──────────────────────────────────────────────────────

    def _refresh_profile_list(self):
        """Repopulate the left-hand sidebar from disk."""
        # Remove all existing rows first
        while self._profile_list.get_first_child():
            self._profile_list.remove(self._profile_list.get_first_child())

        for p in list_deploy_profiles():
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_margin_top(6)
            box.set_margin_bottom(6)
            box.set_margin_start(10)

            name_lbl = Gtk.Label(label=p.name)
            name_lbl.set_xalign(0)
            name_lbl.set_ellipsize(Pango.EllipsizeMode.END)

            count_lbl = Gtk.Label(label=f"{len(p.slots)} slot(s)")
            count_lbl.add_css_class("muted")
            count_lbl.set_xalign(0)

            box.append(name_lbl)
            box.append(count_lbl)
            row.set_child(box)
            # Store name as an attribute for retrieval in _on_profile_row_selected
            row._profile_name = p.name
            self._profile_list.append(row)

    def _on_profile_row_selected(self, lb, row):
        """Load the clicked profile into the editing page."""
        if row is None:
            return
        name = getattr(row, "_profile_name", None)
        if name:
            profile = load_deploy_profile(name)
            if profile:
                self._load_profile_into_editor(profile)

    def _on_new_profile(self):
        """Open the editing page with a blank profile template."""
        self._load_profile_into_editor(DeployProfile(name="new-profile"))

    # ── Editing page helpers ─────────────────────────────────────────────────

    def _load_profile_into_editor(self, profile: DeployProfile):
        """Populate the editing page from a DeployProfile and switch to it."""
        self._current_profile = profile
        self._name_entry.set_text(profile.name)
        self._desc_entry.set_text(profile.description)

        # Clear existing slot rows
        while self._slots_box.get_first_child():
            self._slots_box.remove(self._slots_box.get_first_child())
        self._slot_widgets.clear()

        for slot in profile.slots:
            self._add_slot_row(slot)
        # Always show at least one empty slot row to guide the user
        if not profile.slots:
            self._add_slot_row()

        self._right_stack.set_visible_child_name("editing")

    def _catalog_model_list(self):
        """Return (display_strings, model_keys) for models eligible in slots.

        Eligible models are those with device_type in _ELIGIBLE_DEVICE_TYPES
        (i.e. models that fit on 1–2 chips).  Returns placeholder strings when
        the catalog is not yet loaded.
        """
        catalog = self._get_catalog()
        if catalog is None:
            return ["(catalog not loaded — open a repo first)"], []
        entries = [e for e in catalog.all_entries()
                   if e.device_type in _ELIGIBLE_DEVICE_TYPES]
        display = [f"{e.display_name}  ({e.device_type})" for e in entries]
        keys = [(e.model_name, e.device_type) for e in entries]
        return display, keys

    def _add_slot_row(self, slot: Optional[ProfileSlot] = None):
        """Append one slot editor row, optionally pre-populated from *slot*."""
        display, keys = self._catalog_model_list()

        # Model dropdown — the primary selector for this slot
        model_dd = Gtk.DropDown.new_from_strings(display)
        model_dd.set_hexpand(True)

        # Read-only device label updated automatically when the model changes
        device_lbl = Gtk.Label(label="—")
        device_lbl.set_width_chars(8)

        # Port entry — defaults to 8000, 8001, 8002, … based on slot index
        port_entry = Gtk.Entry()
        port_entry.set_text(str(8000 + len(self._slot_widgets)))
        port_entry.set_max_width_chars(6)
        port_entry.set_width_chars(6)

        # Optional chip pinning — leave blank for auto-assignment
        chip_entry = Gtk.Entry()
        chip_entry.set_placeholder_text("auto")
        chip_entry.set_max_width_chars(5)
        chip_entry.set_width_chars(5)

        # Human-readable label shown in the running view
        label_entry = Gtk.Entry()
        label_entry.set_placeholder_text("label")
        label_entry.set_width_chars(8)

        sw = _SlotWidgets(None, model_dd, device_lbl, port_entry, chip_entry, label_entry, keys)
        self._slot_widgets.append(sw)

        remove_btn = Gtk.Button(label="×")
        remove_btn.add_css_class("flat")
        # Capture sw by object reference so removal works correctly even after
        # earlier slots are deleted (avoids the stale-index bug).
        remove_btn.connect("clicked", lambda _, s=sw: self._remove_slot_row_by_ref(s))

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_top(2)
        row.append(model_dd)
        row.append(device_lbl)
        row.append(port_entry)
        row.append(chip_entry)
        row.append(label_entry)
        row.append(remove_btn)
        sw.row = row
        self._slots_box.append(row)

        # Keep device_lbl in sync with the dropdown selection
        def _on_model_picked(dd, _pspec, lbl=device_lbl, k=keys):
            sel = dd.get_selected()
            if sel != Gtk.INVALID_LIST_POSITION and sel < len(k):
                lbl.set_text(k[sel][1])
        model_dd.connect("notify::selected", _on_model_picked)

        # Pre-fill from an existing slot if provided
        if slot and slot.model_name:
            for i, (mn, dt) in enumerate(keys):
                if mn == slot.model_name and dt == slot.device_type:
                    model_dd.set_selected(i)
                    device_lbl.set_text(dt)
                    break
            port_entry.set_text(slot.port)
            if slot.chip_index is not None:
                chip_entry.set_text(str(slot.chip_index))
            label_entry.set_text(slot.label)

    def _remove_slot_row_by_ref(self, sw: _SlotWidgets):
        """Remove the slot row identified by object reference."""
        if sw not in self._slot_widgets:
            return
        if sw.row and sw.row.get_parent():
            self._slots_box.remove(sw.row)
        self._slot_widgets.remove(sw)

    def _collect_slots(self) -> List[ProfileSlot]:
        """Return ProfileSlots for all valid slot rows (skips rows with no model selected)."""
        return [s for sw in self._slot_widgets if (s := sw.to_slot()) is not None]

    def _on_save(self):
        """Persist the current editing page state to disk and refresh the sidebar."""
        name = self._name_entry.get_text().strip()
        if not name:
            return
        profile = DeployProfile(
            name=name,
            description=self._desc_entry.get_text().strip(),
            slots=self._collect_slots(),
        )
        save_deploy_profile(profile)
        self._current_profile = profile
        self._refresh_profile_list()

    def _on_launch_from_edit(self):
        """Save the current profile then launch it immediately."""
        if not self._name_entry.get_text().strip():
            return
        self._on_save()
        if self._current_profile:
            self._launch_profile(self._current_profile)

    # ── Running page helpers ─────────────────────────────────────────────────

    def _launch_profile(self, profile: DeployProfile):
        """Switch to the running page and start all slots via the orchestrator."""
        catalog = self._get_catalog()
        if catalog is None:
            return

        self._running_header.set_text(f"Running: {profile.name}")

        # Rebuild slot cards for this profile
        while self._cards_box.get_first_child():
            self._cards_box.remove(self._cards_box.get_first_child())
        self._slot_cards = []

        for idx, slot in enumerate(profile.slots):
            card = _SlotCard(idx, slot, stop_cb=lambda i=idx: self._orch.stop_slot(i))
            self._cards_box.append(card)
            self._slot_cards.append(card)

        self._right_stack.set_visible_child_name("running")
        self._orch.launch_profile(profile, catalog)

    # ── Orchestrator callbacks (called on GTK main thread via dispatch_fn) ────

    def _on_slot_state(self, idx: int, state: ServerState, info: str):
        """Update the state badge on the appropriate slot card."""
        if idx < len(self._slot_cards):
            self._slot_cards[idx].set_state(state)

    def _on_slot_log(self, idx: int, line: str):
        """Append a log line to the appropriate slot card's log view."""
        if idx < len(self._slot_cards):
            self._slot_cards[idx].append_log(line)

    def _on_profile_done(self):
        """Called when all slots have finished (DONE or ERROR).

        Currently a no-op; could show a summary dialog in the future.
        """
        pass


class _SlotCard(Gtk.Box):
    """Per-slot status card shown in the running view.

    Displays the slot's title, device type, port, a state badge pill,
    a stop button, and a scrollable log view showing recent output.
    """

    def __init__(self, idx: int, slot: ProfileSlot, stop_cb: Callable):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.add_css_class("model-card")
        self._log_buffer = None
        self._log_scroll = None
        self._build(idx, slot, stop_cb)

    def _build(self, idx: int, slot: ProfileSlot, stop_cb: Callable):
        """Construct the card header and collapsible log view."""
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        # Prefer slot.label; fall back to model_name
        title = slot.label or slot.model_name
        title_lbl = Gtk.Label(label=f"Slot {idx + 1}: {title}")
        title_lbl.add_css_class("model-card-name")
        title_lbl.set_xalign(0)
        title_lbl.set_hexpand(True)
        title_lbl.set_ellipsize(Pango.EllipsizeMode.END)

        dt_lbl = Gtk.Label(label=slot.device_type)
        dt_lbl.add_css_class("muted")

        port_lbl = Gtk.Label(label=f":{slot.port}")
        port_lbl.add_css_class("muted")

        # State badge pill — starts at LAUNCHING, updated by set_state()
        self._state_badge = Gtk.Label(label="LAUNCHING")
        self._state_badge.add_css_class("pill")
        self._state_badge.add_css_class("pill-loading")

        stop_btn = Gtk.Button(label="Stop")
        stop_btn.add_css_class("stop-btn")
        stop_btn.connect("clicked", lambda _: stop_cb())

        header.append(title_lbl)
        header.append(dt_lbl)
        header.append(port_lbl)
        header.append(self._state_badge)
        header.append(stop_btn)
        self.append(header)

        self._log_scroll = Gtk.ScrolledWindow()
        self._log_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._log_scroll.set_size_request(-1, 120)
        self._log_view = Gtk.TextView()
        self._log_view.set_editable(False)
        self._log_view.set_cursor_visible(False)
        self._log_view.add_css_class("log-view")
        self._log_buffer = self._log_view.get_buffer()
        self._log_scroll.set_child(self._log_view)
        # Auto-scroll: wire to "changed" so we scroll after layout is updated.
        self._log_scroll.get_vadjustment().connect(
            "changed", lambda adj: adj.set_value(adj.get_upper() - adj.get_page_size())
        )
        self.append(self._log_scroll)

    def set_state(self, state: ServerState):
        """Update the badge text and CSS class to reflect *state*."""
        label, css = _STATE_CSS.get(state, (state.name, "pill-idle"))
        self._state_badge.set_text(label)
        # Remove all known pill CSS classes before adding the new one
        for _, old_css in _STATE_CSS.values():
            self._state_badge.remove_css_class(old_css)
        self._state_badge.add_css_class(css)

    def append_log(self, line: str):
        """Insert *line* at the end of the log buffer."""
        if self._log_buffer is None:
            return
        end_iter = self._log_buffer.get_end_iter()
        self._log_buffer.insert(end_iter, line + "\n")
