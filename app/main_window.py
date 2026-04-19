#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Main application window: sidebar + main panel (thin view over AppController).

MainWindow is a pure GTK view.  It owns no business logic — all server
lifecycle, device detection, and progress tracking live in AppController.

Threading discipline (CRITICAL):
    GTK is single-threaded. Worker threads must NEVER touch widgets directly.
    AppController dispatches every on_* callback through GLib.idle_add so that
    all widget updates happen on the GTK main thread.
"""
import os
from pathlib import Path
from typing import List, Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import GLib, Gtk, Pango

from app_settings import settings as _settings
from model_catalog import ModelCatalog, ModelEntry
from server_manager import ServerState

_TYPE_ORDER = ["LLM", "VLM", "IMAGE", "VIDEO", "AUDIO", "CNN", "EMBEDDING", "TTS"]
_TYPE_LABEL = {
    "LLM": "LLM", "VLM": "VLM", "IMAGE": "Image", "VIDEO": "Video",
    "AUDIO": "Audio", "CNN": "CNN", "EMBEDDING": "Embedding", "TTS": "TTS",
}
_DEVICE_ORDER = ["N150", "N300", "P100", "P150", "P150X4", "P300", "P300X2", "T3K", "P150X8"]

_STATE_LABELS = {
    ServerState.IDLE:          ("IDLE",          "pill-idle"),
    ServerState.LAUNCHING:     ("LAUNCHING",     "pill-loading"),
    ServerState.PULLING_IMAGE: ("PULLING IMAGE", "pill-loading"),
    ServerState.LOADING:       ("LOADING",       "pill-loading"),
    ServerState.READY:         ("READY",         "pill-ready"),
    ServerState.ERROR:         ("ERROR",         "pill-error"),
    ServerState.STOPPING:      ("STOPPING",      "pill-stopping"),
}

_LOG_COLORS = {
    "DEBUG":    "#607D8B",
    "INFO":     "#E8F0F2",
    "WARN":     "#F4C471",
    "WARNING":  "#F4C471",
    "ERROR":    "#FF6B6B",
    "CRITICAL": "#FF6B6B",
}


class Sidebar(Gtk.Box):
    """Left sidebar: repo path picker, model tree, device toggles, port, launch/stop, HF status."""

    def __init__(self, on_launch, on_stop, on_model_select, on_device_select, on_repo_change):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_size_request(290, -1)

        self._on_launch = on_launch
        self._on_stop = on_stop
        self._on_model_select = on_model_select
        self._on_device_select = on_device_select
        self._on_repo_change = on_repo_change

        self._catalog: Optional[ModelCatalog] = None
        self._selected_entry: Optional[ModelEntry] = None
        self._selected_device: Optional[str] = None
        self._device_buttons: dict = {}
        self._locked = False
        self._launch_connected_to_launch = True

        self._build()

    def _build(self):
        # Repo path
        rbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        rbox.set_margin_start(8); rbox.set_margin_end(8)
        rbox.set_margin_top(8);   rbox.set_margin_bottom(4)
        rl = Gtk.Label(label="SERVER REPO"); rl.add_css_class("section-label")
        rl.set_halign(Gtk.Align.START); rbox.append(rl)
        self._repo_entry = Gtk.Entry()
        self._repo_entry.set_placeholder_text("~/code/tt-inference-server")
        saved = _settings.server_repo_path
        if saved: self._repo_entry.set_text(str(saved))
        self._repo_entry.connect("activate", lambda e: self._trigger_repo_change())
        rbox.append(self._repo_entry)
        self.append(rbox)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Model section label
        ml_box = Gtk.Box(); ml_box.set_margin_start(8); ml_box.set_margin_top(6); ml_box.set_margin_bottom(2)
        ml = Gtk.Label(label="MODEL"); ml.add_css_class("section-label"); ml.set_halign(Gtk.Align.START)
        ml_box.append(ml); self.append(ml_box)

        # Model tree
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._tree_store = Gtk.TreeStore(str, str, str, bool)  # display, model_key, device_type, is_leaf
        self._tree_view = Gtk.TreeView(model=self._tree_store)
        self._tree_view.set_headers_visible(False)
        self._tree_view.set_activate_on_single_click(False)
        col = Gtk.TreeViewColumn("Model", Gtk.CellRendererText(), text=0, sensitive=3)
        self._tree_view.append_column(col)
        self._tree_view.get_selection().connect("changed", self._on_tree_selection)
        scroll.set_child(self._tree_view)
        self.append(scroll)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Device buttons
        dbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        dbox.set_margin_start(8); dbox.set_margin_end(8)
        dbox.set_margin_top(6);   dbox.set_margin_bottom(4)
        dl = Gtk.Label(label="DEVICE"); dl.add_css_class("section-label"); dl.set_halign(Gtk.Align.START)
        dbox.append(dl)
        self._device_flow = Gtk.FlowBox()
        self._device_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._device_flow.set_max_children_per_line(4)
        dbox.append(self._device_flow)
        self.append(dbox)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Port
        pbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        pbox.set_margin_start(8); pbox.set_margin_end(8)
        pbox.set_margin_top(4);   pbox.set_margin_bottom(4)
        pl = Gtk.Label(label="PORT"); pl.add_css_class("section-label"); pbox.append(pl)
        self._port_entry = Gtk.Entry()
        self._port_entry.set_text(_settings.last_port or "8000")
        self._port_entry.set_hexpand(True)
        self._port_entry.connect("changed", lambda e: self._save_port())
        pbox.append(self._port_entry)
        self.append(pbox)

        # Launch button
        btnbox = Gtk.Box(); btnbox.set_margin_start(8); btnbox.set_margin_end(8)
        btnbox.set_margin_top(4); btnbox.set_margin_bottom(4)
        self._launch_btn = Gtk.Button(label="▶  Launch Server")
        self._launch_btn.add_css_class("launch-btn")
        self._launch_btn.set_hexpand(True)
        self._launch_btn.connect("clicked", self._on_launch_clicked)
        btnbox.append(self._launch_btn)
        self.append(btnbox)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # HF token status
        self._hf_label = Gtk.Label()
        self._hf_label.set_margin_start(8); self._hf_label.set_margin_top(4); self._hf_label.set_margin_bottom(6)
        self._hf_label.set_halign(Gtk.Align.START)
        self.append(self._hf_label)
        self._update_hf_status()

    def _save_port(self):
        _settings.last_port = self._port_entry.get_text()
        _settings.save()

    def _trigger_repo_change(self):
        text = self._repo_entry.get_text()
        path = Path(text).expanduser()
        _settings.server_repo_path = str(path)
        _settings.save()
        self._on_repo_change(path)
        self._update_hf_status()

    def _update_hf_status(self):
        token = os.environ.get("HF_TOKEN", "")
        if not token:
            repo = self._repo_entry.get_text()
            if repo:
                env_file = Path(repo).expanduser() / ".env"
                if env_file.exists():
                    for line in env_file.read_text().splitlines():
                        if line.startswith("HF_TOKEN="):
                            token = line.split("=", 1)[1].strip()
                            break
        if token:
            self._hf_label.set_text("HF_TOKEN: ✓ from env")
            self._hf_label.set_css_classes(["hf-ok"])
            if not self._locked:
                self._launch_btn.set_sensitive(True)
        else:
            self._hf_label.set_text("⚠ HF_TOKEN not set — Launch disabled")
            self._hf_label.set_css_classes(["hf-warn"])
            self._launch_btn.set_sensitive(False)
            self._launch_btn.set_tooltip_text("Set HF_TOKEN in environment or .env file in server repo")

    def load_catalog(self, catalog: ModelCatalog, compatible_devices: List[str]):
        self._catalog = catalog
        self._build_device_buttons(catalog.all_device_types(), compatible_devices)
        active = self._selected_device or (compatible_devices[0] if compatible_devices else None)
        self._rebuild_tree([active] if active else compatible_devices)

    def _build_device_buttons(self, all_devices: List[str], compatible: List[str]):
        while child := self._device_flow.get_first_child():
            self._device_flow.remove(child)
        self._device_buttons.clear()

        ordered = [d for d in _DEVICE_ORDER if d in all_devices] + [d for d in all_devices if d not in _DEVICE_ORDER]
        last = _settings.last_device

        for dev in ordered:
            btn = Gtk.ToggleButton(label=dev)
            is_compat = dev in compatible or not compatible
            btn.set_sensitive(is_compat)
            if not compatible:
                btn.set_tooltip_text("tt-smi not found — showing all devices")
            self._device_flow.append(btn)
            self._device_buttons[dev] = btn

            active = (dev == last) or (not last and dev == ordered[0] and is_compat)
            if active and not self._selected_device:
                btn.set_active(True)
                self._selected_device = dev
            btn.connect("toggled", self._on_device_toggled, dev)

    def _on_device_toggled(self, btn, device):
        if not btn.get_active():
            return
        for d, b in self._device_buttons.items():
            if d != device:
                b.handler_block_by_func(self._on_device_toggled)
                b.set_active(False)
                b.handler_unblock_by_func(self._on_device_toggled)
        self._selected_device = device
        _settings.last_device = device
        _settings.save()
        self._rebuild_tree([device])
        self._on_device_select(device)

    def _rebuild_tree(self, filter_devices: Optional[List[str]]):
        self._tree_store.clear()
        if not self._catalog:
            return
        cat = self._catalog.get_compatible(filter_devices) if filter_devices else self._catalog
        tree = cat.get_tree()
        expanded = _settings.tree_expanded_types or ["LLM"]
        last_model = _settings.last_model

        for type_name in _TYPE_ORDER:
            if type_name not in tree:
                continue
            families = tree[type_name]
            total = sum(len(v) for v in families.values())
            type_it = self._tree_store.append(
                None, [f"{_TYPE_LABEL.get(type_name, type_name)} ({total})", "", "", False]
            )
            for family, entries in sorted(families.items()):
                fam_it = self._tree_store.append(type_it, [family, "", "", False])
                for entry in entries:
                    leaf_it = self._tree_store.append(
                        fam_it, [entry.display_name, entry.model_name, entry.device_type, True]
                    )
                    if entry.model_name == last_model:
                        self._tree_view.get_selection().select_iter(leaf_it)
            if type_name in expanded:
                self._tree_view.expand_row(self._tree_store.get_path(type_it), False)

    def _on_tree_selection(self, sel):
        model, it = sel.get_selected()
        if it is None:
            return
        if not model.get_value(it, 3):  # not a leaf
            return
        model_key = model.get_value(it, 1)
        device = model.get_value(it, 2)
        if self._catalog:
            entry = self._catalog.get_entry(model_key, device)
            if entry:
                self._selected_entry = entry
                _settings.last_model = model_key
                _settings.last_device = device
                _settings.save()
                self._on_model_select(entry)

    def _on_launch_clicked(self, btn):
        if self._selected_entry:
            self._on_launch(self._selected_entry, self._port_entry.get_text() or "8000")

    def set_locked(self, locked: bool):
        self._locked = locked
        self._tree_view.set_sensitive(not locked)
        self._repo_entry.set_sensitive(not locked)
        for btn in self._device_buttons.values():
            btn.set_sensitive(not locked)
        self._port_entry.set_sensitive(not locked)

        if locked:
            self._launch_btn.set_label("■  Stop Server")
            self._launch_btn.set_css_classes(["stop-btn"])
            self._launch_btn.set_sensitive(True)
            if self._launch_connected_to_launch:
                self._launch_btn.disconnect_by_func(self._on_launch_clicked)
                self._launch_btn.connect("clicked", self._on_stop_clicked)
                self._launch_connected_to_launch = False
        else:
            self._launch_btn.set_label("▶  Launch Server")
            self._launch_btn.set_css_classes(["launch-btn"])
            if not self._launch_connected_to_launch:
                self._launch_btn.disconnect_by_func(self._on_stop_clicked)
                self._launch_btn.connect("clicked", self._on_launch_clicked)
                self._launch_connected_to_launch = True
            self._update_hf_status()

    def _on_stop_clicked(self, btn):
        self._on_stop()

    def get_selected_entry(self) -> Optional[ModelEntry]:
        return self._selected_entry

    def get_port(self) -> str:
        return self._port_entry.get_text() or "8000"


_LOG_LEVELS_ORDERED = ["DEBUG", "INFO", "WARN", "ERROR"]
_MAX_LOG_ENTRIES = 5000


class MainPanel(Gtk.Box):
    """Right panel: status banner, sub-stage stepper, progress bar, tour panel, log view."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._auto_scroll = True
        self._pulse_source: Optional[int] = None
        self._log_entries: list = []        # (line_text, level_str) tuples
        self._hidden_levels: set = set()
        self._build()

    def _build(self):
        # Status banner
        banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        banner.set_margin_start(10); banner.set_margin_end(10)
        banner.set_margin_top(6);   banner.set_margin_bottom(6)
        self._pill = Gtk.Label(label="IDLE")
        self._pill.set_css_classes(["pill", "pill-idle"])
        banner.append(self._pill)
        self._banner_info = Gtk.Label(label="Select a model and click Launch")
        self._banner_info.add_css_class("muted")
        self._banner_info.set_hexpand(True)
        self._banner_info.set_halign(Gtk.Align.START)
        self._banner_info.set_ellipsize(Pango.EllipsizeMode.END)
        banner.append(self._banner_info)
        self.append(banner)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Stepper (revealed during LOADING)
        self._stepper_rev = Gtk.Revealer()
        self._stepper_rev.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        stepper_box = Gtk.Box(); stepper_box.set_margin_start(10); stepper_box.set_margin_top(5); stepper_box.set_margin_bottom(3)
        self._stepper_label = Gtk.Label(label="")
        self._stepper_label.add_css_class("muted")
        self._stepper_label.set_halign(Gtk.Align.START)
        stepper_box.append(self._stepper_label)
        self._stepper_rev.set_child(stepper_box)
        self.append(self._stepper_rev)

        # Progress bar + label (revealed during active states)
        self._progress_rev = Gtk.Revealer()
        self._progress_rev.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        prog_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        prog_box.set_margin_start(10); prog_box.set_margin_end(10)
        prog_box.set_margin_top(4);    prog_box.set_margin_bottom(4)
        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_pulse_step(0.07)
        prog_box.append(self._progress_bar)
        self._progress_label = Gtk.Label(label="")
        self._progress_label.add_css_class("muted")
        self._progress_label.set_halign(Gtk.Align.START)
        prog_box.append(self._progress_label)
        self._progress_rev.set_child(prog_box)
        self.append(self._progress_rev)

        # Tour panel (revealed during LOADING)
        self._tour_rev = Gtk.Revealer()
        self._tour_rev.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        tour_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tour_outer.add_css_class("tour-panel")
        tour_outer.set_margin_start(8); tour_outer.set_margin_end(8)
        tour_outer.set_margin_top(4);   tour_outer.set_margin_bottom(4)
        tour_inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        tour_inner.set_size_request(-1, 110)

        self._tour_left = Gtk.Label(label="")
        self._tour_left.add_css_class("muted")
        self._tour_left.set_halign(Gtk.Align.START); self._tour_left.set_valign(Gtk.Align.START)
        self._tour_left.set_margin_start(6); self._tour_left.set_margin_top(4)
        self._tour_left.set_hexpand(True)
        tour_inner.append(self._tour_left)
        tour_inner.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self._tour_right = Gtk.Label(label="")
        self._tour_right.set_wrap(True)
        self._tour_right.set_halign(Gtk.Align.START); self._tour_right.set_valign(Gtk.Align.START)
        self._tour_right.set_margin_start(8); self._tour_right.set_margin_end(6); self._tour_right.set_margin_top(4)
        self._tour_right.set_hexpand(True)
        tour_inner.append(self._tour_right)
        # Tour dot indicator (card N of M)
        tour_dots_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        tour_dots_box.set_halign(Gtk.Align.CENTER)
        tour_dots_box.set_margin_top(2); tour_dots_box.set_margin_bottom(4)
        self._tour_dots = Gtk.Label(label="")
        self._tour_dots.add_css_class("muted")
        tour_dots_box.append(self._tour_dots)
        tour_outer.append(tour_inner)
        tour_outer.append(tour_dots_box)
        self._tour_rev.set_child(tour_outer)
        self.append(self._tour_rev)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # ── Ready-state tab bar ───────────────────────────────────────────────
        # Shown only when state == READY to switch between logs/tools/bench pages.
        self._tab_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._tab_bar.set_margin_start(8); self._tab_bar.set_margin_end(8)
        self._tab_bar.set_margin_top(3);   self._tab_bar.set_margin_bottom(3)
        self._tab_btns: dict = {}
        for tab_id, tab_label in [("logs", "Logs"), ("tools", "Tools"), ("bench", "Bench")]:
            btn = Gtk.ToggleButton(label=tab_label)
            btn.add_css_class("log-filter-btn")
            btn.connect("toggled", self._on_tab_toggled, tab_id)
            self._tab_bar.append(btn)
            self._tab_btns[tab_id] = btn
        self._tab_bar.set_visible(False)
        self.append(self._tab_bar)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Stack — holds welcome / config / logs pages.
        # The log filter toolbar and log text view live inside the "logs" page
        # so they are only visible when a server is actively running.
        self._stack = Gtk.Stack()
        self._stack.set_vexpand(True)
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(150)

        # ── Welcome page ─────────────────────────────────────────────────────
        # Shown on startup before the user selects a model.
        welcome_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        welcome_box.set_valign(Gtk.Align.CENTER)
        welcome_box.set_halign(Gtk.Align.CENTER)
        welcome_lbl = Gtk.Label(label="Select a model to configure and launch")
        welcome_lbl.add_css_class("muted")
        welcome_box.append(welcome_lbl)
        self._stack.add_named(welcome_box, "welcome")

        # ── Config page ───────────────────────────────────────────────────────
        # Created lazily on first show_config() call to avoid importing GTK
        # widgets before the window is fully constructed.
        self._config_panel = None

        # ── Logs page ─────────────────────────────────────────────────────────
        # Contains the filter toolbar and the scrollable log text view.
        # Shown automatically when the server is launched.
        logs_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Log filter toolbar
        filter_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        filter_bar.set_margin_start(8); filter_bar.set_margin_end(8)
        filter_bar.set_margin_top(3);   filter_bar.set_margin_bottom(3)
        filter_lbl = Gtk.Label(label="Filter:")
        filter_lbl.add_css_class("muted")
        filter_bar.append(filter_lbl)
        self._filter_btns: dict = {}
        for lvl in _LOG_LEVELS_ORDERED:
            btn = Gtk.ToggleButton(label=lvl)
            btn.set_active(True)
            btn.add_css_class("log-filter-btn")
            btn.connect("toggled", self._on_filter_toggled, lvl)
            filter_bar.append(btn)
            self._filter_btns[lvl] = btn
        self._log_count_lbl = Gtk.Label(label="")
        self._log_count_lbl.add_css_class("muted")
        self._log_count_lbl.set_hexpand(True)
        self._log_count_lbl.set_halign(Gtk.Align.END)
        filter_bar.append(self._log_count_lbl)
        logs_box.append(filter_bar)
        logs_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Log text view inside a scrolled window
        log_scroll = Gtk.ScrolledWindow()
        log_scroll.set_vexpand(True)
        log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        self._log_buf = Gtk.TextBuffer()
        for level, color in _LOG_COLORS.items():
            self._log_buf.create_tag(f"lvl_{level}", foreground=color)
        self._log_buf.create_tag("ts", foreground="#4FD1C5")

        self._log_view = Gtk.TextView(buffer=self._log_buf)
        self._log_view.set_editable(False)
        self._log_view.set_cursor_visible(False)
        self._log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._log_view.add_css_class("log-view")
        self._log_view.set_monospace(True)

        self._vadj = log_scroll.get_vadjustment()
        self._vadj.connect("value-changed", self._on_scroll)
        self._vadj.connect("changed",       self._on_adj_changed)

        log_scroll.set_child(self._log_view)
        logs_box.append(log_scroll)
        self._stack.add_named(logs_box, "logs")

        # ── Tools page ────────────────────────────────────────────────────────
        # Two-column layout: left = editable tool definition JSON, right = prompt
        # entry + Send button + round-trip output view.
        tools_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        tools_box.set_margin_start(12); tools_box.set_margin_end(12)
        tools_box.set_margin_top(8);    tools_box.set_margin_bottom(8)

        # Hint label shown when tool use was not enabled at launch.
        self._tool_hint = Gtk.Label(
            label="Tool use was not enabled at launch.\nRe-launch with 🔧 Tool use toggled on."
        )
        self._tool_hint.add_css_class("muted")
        self._tool_hint.set_justify(Gtk.Justification.CENTER)

        # Two-column area fills the remaining vertical space.
        tools_cols = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        tools_cols.set_vexpand(True)

        # Left column: editable tool-definition JSON (pre-filled with weather example).
        left_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        left_col.set_hexpand(True)
        tool_def_lbl = Gtk.Label(label="TOOL DEFINITION (JSON)")
        tool_def_lbl.add_css_class("muted")
        tool_def_lbl.set_halign(Gtk.Align.START)
        left_col.append(tool_def_lbl)
        self._tool_def_buf = Gtk.TextBuffer()
        self._tool_def_buf.set_text(
            '[\n  {\n    "type": "function",\n'
            '    "function": {\n'
            '      "name": "get_weather",\n'
            '      "description": "Get current weather for a city",\n'
            '      "parameters": {\n'
            '        "type": "object",\n'
            '        "properties": {"city": {"type": "string"}},\n'
            '        "required": ["city"]\n'
            '      }\n    }\n  }\n]'
        )
        tool_def_view = Gtk.TextView(buffer=self._tool_def_buf)
        tool_def_view.set_monospace(True)
        tool_def_view.add_css_class("log-view")
        tool_def_scroll = Gtk.ScrolledWindow()
        tool_def_scroll.set_vexpand(True)
        tool_def_scroll.set_child(tool_def_view)
        left_col.append(tool_def_scroll)
        tools_cols.append(left_col)

        # Right column: prompt entry, send button, and round-trip output viewer.
        right_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right_col.set_hexpand(True)
        prompt_lbl = Gtk.Label(label="PROMPT")
        prompt_lbl.add_css_class("muted")
        prompt_lbl.set_halign(Gtk.Align.START)
        right_col.append(prompt_lbl)
        self._tool_prompt_entry = Gtk.Entry()
        self._tool_prompt_entry.set_placeholder_text("What's the weather in Austin?")
        right_col.append(self._tool_prompt_entry)
        self._tool_send_btn = Gtk.Button(label="▶ Send")
        self._tool_send_btn.add_css_class("suggested-action")
        right_col.append(self._tool_send_btn)
        output_lbl = Gtk.Label(label="ROUND-TRIP")
        output_lbl.add_css_class("muted")
        output_lbl.set_halign(Gtk.Align.START)
        right_col.append(output_lbl)
        self._tool_output_buf = Gtk.TextBuffer()
        tool_output_view = Gtk.TextView(buffer=self._tool_output_buf)
        tool_output_view.set_editable(False)
        tool_output_view.set_monospace(True)
        tool_output_view.add_css_class("log-view")
        tool_output_scroll = Gtk.ScrolledWindow()
        tool_output_scroll.set_vexpand(True)
        tool_output_scroll.set_child(tool_output_view)
        right_col.append(tool_output_scroll)
        tools_cols.append(right_col)

        tools_box.append(self._tool_hint)
        tools_box.append(tools_cols)
        self._stack.add_named(tools_box, "tools")

        # ── Bench page ────────────────────────────────────────────────────────
        bench_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        bench_box.set_margin_start(12); bench_box.set_margin_end(12)
        bench_box.set_margin_top(8);    bench_box.set_margin_bottom(8)

        run_cfg_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        mode_lbl = Gtk.Label(label="Mode:")
        mode_lbl.add_css_class("muted")
        run_cfg_box.append(mode_lbl)
        self._bench_mode_combo = Gtk.ComboBoxText()
        for m in ["smoke-test", "ci-nightly", "ci-long"]:
            self._bench_mode_combo.append_text(m)
        self._bench_mode_combo.set_active(0)
        run_cfg_box.append(self._bench_mode_combo)
        self._bench_sweeps_check = Gtk.CheckButton(label="Concurrency sweeps")
        run_cfg_box.append(self._bench_sweeps_check)
        self._bench_pct_check = Gtk.CheckButton(label="Percentile report")
        run_cfg_box.append(self._bench_pct_check)
        self._bench_run_btn = Gtk.Button(label="▶ Run Benchmark")
        self._bench_run_btn.add_css_class("suggested-action")
        run_cfg_box.append(self._bench_run_btn)
        bench_box.append(run_cfg_box)
        bench_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        bench_log_lbl = Gtk.Label(label="LIVE OUTPUT")
        bench_log_lbl.add_css_class("muted")
        bench_log_lbl.set_halign(Gtk.Align.START)
        bench_box.append(bench_log_lbl)
        self._bench_log_buf = Gtk.TextBuffer()
        bench_log_view = Gtk.TextView(buffer=self._bench_log_buf)
        bench_log_view.set_editable(False)
        bench_log_view.set_monospace(True)
        bench_log_view.add_css_class("log-view")
        bench_log_scroll = Gtk.ScrolledWindow()
        bench_log_scroll.set_vexpand(True)
        bench_log_scroll.set_child(bench_log_view)
        bench_box.append(bench_log_scroll)

        bench_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        bench_results_lbl = Gtk.Label(label="RESULTS")
        bench_results_lbl.add_css_class("muted")
        bench_results_lbl.set_halign(Gtk.Align.START)
        bench_box.append(bench_results_lbl)
        self._bench_results_buf = Gtk.TextBuffer()
        bench_results_view = Gtk.TextView(buffer=self._bench_results_buf)
        bench_results_view.set_editable(False)
        bench_results_view.set_monospace(True)
        bench_results_view.add_css_class("log-view")
        bench_results_scroll = Gtk.ScrolledWindow()
        bench_results_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        bench_results_scroll.set_size_request(-1, 120)
        bench_results_scroll.set_child(bench_results_view)
        bench_box.append(bench_results_scroll)

        self._stack.add_named(bench_box, "bench")

        self.append(self._stack)

    # ---------------------------------------------------------------- stack navigation

    def show_welcome(self) -> None:
        """Switch the main content area to the welcome splash page."""
        self._stack.set_visible_child_name("welcome")

    def show_config(self, entry, on_options_changed) -> None:
        """Switch to the config page, creating ConfigPanel lazily on first call.

        The ConfigPanel import is deferred here so that the widget is only
        constructed after the full GTK window hierarchy is in place.
        """
        from config_panel import ConfigPanel
        if self._config_panel is None:
            self._config_panel = ConfigPanel(on_options_changed)
            self._stack.add_named(self._config_panel, "config")
        self._config_panel.set_model(entry)
        self._stack.set_visible_child_name("config")

    def show_logs(self) -> None:
        """Switch the main content area to the live log view."""
        self._stack.set_visible_child_name("logs")

    def get_options(self):
        """Return the current LaunchOptions from ConfigPanel, or None if not yet created."""
        if self._config_panel is not None:
            return self._config_panel.get_options()
        return None

    def _on_scroll(self, adj):
        self._auto_scroll = adj.get_value() >= adj.get_upper() - adj.get_page_size() - 10

    def _on_adj_changed(self, adj):
        if self._auto_scroll:
            adj.set_value(adj.get_upper() - adj.get_page_size())

    def _detect_level(self, line: str) -> str:
        import re
        for lvl in ("ERROR", "CRITICAL", "WARN", "WARNING", "INFO", "DEBUG"):
            if re.search(rf'\b{lvl}\b', line):
                # Normalise WARNING→WARN, CRITICAL→ERROR for filter purposes
                return "ERROR" if lvl == "CRITICAL" else ("WARN" if lvl == "WARNING" else lvl)
        return ""

    def _insert_line_to_buffer(self, line: str, level: str):
        buf = self._log_buf
        end = buf.get_end_iter()
        if buf.get_char_count() > 0:
            buf.insert(end, "\n")
            end = buf.get_end_iter()
        start_off = end.get_offset()
        buf.insert(end, line)
        tag_name = f"lvl_{level}" if level else None
        if tag_name:
            s = buf.get_iter_at_offset(start_off)
            buf.apply_tag_by_name(tag_name, s, buf.get_end_iter())

    def append_log(self, line: str):
        level = self._detect_level(line)
        # Store (capped at _MAX_LOG_ENTRIES)
        self._log_entries.append((line, level))
        if len(self._log_entries) > _MAX_LOG_ENTRIES:
            self._log_entries = self._log_entries[-_MAX_LOG_ENTRIES:]
        # Only insert if this level isn't hidden
        if level not in self._hidden_levels:
            self._insert_line_to_buffer(line, level)
        self._update_log_count()

    def set_state(self, state: ServerState, info: str = ""):
        label, css_class = _STATE_LABELS.get(state, ("?", "pill-idle"))
        self._pill.set_css_classes(["pill", css_class])
        self._pill.set_text(label)
        if info:
            self._banner_info.set_text(info)

        loading = state in (ServerState.LOADING, ServerState.LAUNCHING, ServerState.PULLING_IMAGE)
        active  = state not in (ServerState.IDLE, ServerState.ERROR)

        self._stepper_rev.set_reveal_child(loading)
        self._progress_rev.set_reveal_child(active and state != ServerState.READY)
        self._tour_rev.set_reveal_child(loading)

        # Show tab bar only when READY; default to Logs tab when first entering READY.
        ready = (state == ServerState.READY)
        self._tab_bar.set_visible(ready)
        if ready:
            self._stack.set_visible_child_name("logs")
            self._update_tab_buttons("logs")

        if state in (ServerState.LAUNCHING, ServerState.PULLING_IMAGE):
            if not self._pulse_source:
                self._pulse_source = GLib.timeout_add(120, self._pulse_tick)
        else:
            if self._pulse_source:
                GLib.source_remove(self._pulse_source)
                self._pulse_source = None

        if state == ServerState.READY:
            self._progress_bar.set_fraction(1.0)
            GLib.timeout_add(2000, lambda: self._progress_rev.set_reveal_child(False) or False)

    def _pulse_tick(self) -> bool:
        self._progress_bar.pulse()
        return True

    def set_progress(self, fraction: float, label: str = ""):
        if self._pulse_source:
            GLib.source_remove(self._pulse_source)
            self._pulse_source = None
        self._progress_bar.set_fraction(min(fraction, 0.95))
        self._progress_label.set_text(label)

    def set_stepper(self, text: str):
        self._stepper_label.set_text(text)

    def set_tour(self, left: str, right: str):
        self._tour_left.set_text(left)
        self._tour_right.set_text(right)

    def clear_log(self):
        self._log_buf.set_text("")
        self._log_entries.clear()
        self._auto_scroll = True
        self._update_log_count()

    def _rebuild_log_buffer(self):
        self._log_buf.set_text("")
        self._auto_scroll = True
        for line, level in self._log_entries:
            if level not in self._hidden_levels:
                self._insert_line_to_buffer(line, level)
        self._update_log_count()

    def _update_log_count(self):
        visible = sum(
            1 for _, lvl in self._log_entries if lvl not in self._hidden_levels
        )
        total = len(self._log_entries)
        if visible == total:
            self._log_count_lbl.set_text(f"{total} lines")
        else:
            self._log_count_lbl.set_text(f"{visible}/{total} lines")

    def _on_filter_toggled(self, btn, level: str):
        if btn.get_active():
            self._hidden_levels.discard(level)
        else:
            self._hidden_levels.add(level)
        self._rebuild_log_buffer()

    def update_tour_dots(self, idx: int, total: int):
        if total <= 1:
            self._tour_dots.set_text("")
            return
        dots = "  ".join("●" if i == idx else "○" for i in range(total))
        self._tour_dots.set_text(dots)

    # ---------------------------------------------------------------- tab navigation

    def show_tools(self) -> None:
        """Switch to the tools tab page."""
        self._stack.set_visible_child_name("tools")
        self._update_tab_buttons("tools")

    def show_bench(self) -> None:
        """Switch to the bench tab page."""
        self._stack.set_visible_child_name("bench")
        self._update_tab_buttons("bench")

    def show_logs_tab(self) -> None:
        """Switch to logs page via tab (only updates the tab highlight)."""
        self._stack.set_visible_child_name("logs")
        self._update_tab_buttons("logs")

    def _update_tab_buttons(self, active_id: str) -> None:
        """Set exactly one tab button as active, suppressing toggled signals
        to avoid recursive calls."""
        for tid, btn in self._tab_btns.items():
            btn.handler_block_by_func(self._on_tab_toggled)
            btn.set_active(tid == active_id)
            btn.handler_unblock_by_func(self._on_tab_toggled)

    def _on_tab_toggled(self, btn: Gtk.ToggleButton, tab_id: str) -> None:
        """Handle a tab toggle button click — switch the stack page and update
        the button highlights."""
        if not btn.get_active():
            return
        self._stack.set_visible_child_name(tab_id)
        self._update_tab_buttons(tab_id)

    def set_tool_use_hint_visible(self, visible: bool) -> None:
        """Show/hide the 'tool use not enabled' hint in the tools page."""
        self._tool_hint.set_visible(visible)

    def append_bench_progress(self, line: str) -> None:
        """Append a live log line to the bench live-output buffer."""
        buf = self._bench_log_buf
        end = buf.get_end_iter()
        if buf.get_char_count() > 0:
            buf.insert(end, "\n")
            end = buf.get_end_iter()
        buf.insert(end, line)

    def append_bench_result(self, result) -> None:
        """Append a formatted BenchResult row to the bench results buffer."""
        icon = {"PASS": "✓", "BELOW_TARGET": "⚠", "FAIL": "✗"}.get(result.tier_pass, "?")
        line = (
            f"{icon} {result.tier_pass:12s}"
            f"  ISL={result.isl} OSL={result.osl} Con={result.concurrency}"
            f"  TTFT={result.mean_ttft_ms:.0f}ms"
            f"  TPS={result.mean_tps:.1f}"
            f"  E2EL={result.mean_e2el_ms:.0f}ms"
            f"  [{result.timestamp}]"
        )
        buf = self._bench_results_buf
        end = buf.get_end_iter()
        if buf.get_char_count() > 0:
            buf.insert(end, "\n")
            end = buf.get_end_iter()
        buf.insert(end, line)

    def append_tool_result(self, rt) -> None:
        """Append a ToolRoundTrip step to the round-trip output buffer.

        Each step type is rendered differently:
          - 'call':   outgoing tool call with name and arguments
          - 'result': tool result returned to the model
          - other:    final assistant message (after all tool turns)
        """
        buf = self._tool_output_buf
        end = buf.get_end_iter()
        if buf.get_char_count() > 0:
            buf.insert(end, "\n")
            end = buf.get_end_iter()
        if rt.step == "call":
            buf.insert(end, f"→ tool_call: {rt.name}({rt.arguments})")
        elif rt.step == "result":
            buf.insert(end, f"← tool result: {rt.content}")
        else:
            buf.insert(end, f"← final: {rt.content}")


class MainWindow(Gtk.ApplicationWindow):
    """Thin GTK view wired to an AppController.

    MainWindow owns no business logic.  It builds the widget tree, registers
    on_* callbacks on the controller, and translates user actions into
    controller method calls.  All server lifecycle, device detection, timing,
    and progress tracking live in AppController.
    """

    def __init__(self, controller, **kwargs):
        super().__init__(
            title="TT Model Runner",
            default_width=_settings.window_width,
            default_height=_settings.window_height,
            **kwargs,
        )
        self._ctrl = controller

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(_settings.sidebar_width)

        self._sidebar = Sidebar(
            on_launch=self._on_launch_clicked,
            on_stop=lambda: self._ctrl.stop(),
            on_model_select=self._on_model_select,
            on_device_select=lambda d: None,
            on_repo_change=self._on_repo_change,
        )
        paned.set_start_child(self._sidebar)
        paned.set_resize_start_child(False)

        self._panel = MainPanel()
        paned.set_end_child(self._panel)
        self.set_child(paned)
        self.connect("close-request", self._on_close)

        # Register view callbacks so the controller can push updates to us.
        # The panel exists at this point, so the log callback is safe to set.
        controller.on_state_changed  = self._on_state_changed
        controller.on_log_line       = self._panel.append_log
        controller.on_progress       = self._on_progress
        controller.on_substage       = self._on_substage
        controller.on_catalog_loaded = self._on_catalog_loaded
        controller.on_cache_scanned  = lambda info: None   # no cache UI in this view
        controller.on_bench_progress = self._on_bench_progress
        controller.on_bench_result   = self._on_bench_result
        controller.on_tool_result    = self._on_tool_result

        # Auto-discover and load the inference-server repo on startup.
        # Prefer the path saved from the last session; fall back to well-known
        # checkout locations if the saved path is stale or absent.
        repo_path = None
        saved = _settings.server_repo_path
        if saved:
            p = Path(saved)
            if (p / "run.py").exists() and (p / "model_spec.json").exists():
                repo_path = p
        if not repo_path:
            for candidate in [
                Path.home() / "code" / "tt-inference-server",
                Path.home() / "tt-inference-server",
            ]:
                if (candidate / "run.py").exists() and (candidate / "model_spec.json").exists():
                    repo_path = candidate
                    break
        if repo_path:
            self._sidebar._repo_entry.set_text(str(repo_path))
            GLib.idle_add(self._ctrl.load_repo, repo_path)

    # ── Callbacks pushed by AppController ────────────────────────────────────

    def _on_state_changed(self, state: ServerState, info: str) -> None:
        """React to server state transitions: update banner, lock sidebar,
        and navigate the main panel stack to the appropriate page."""
        self._panel.set_state(state, info)
        self._sidebar.set_locked(state not in (ServerState.IDLE, ServerState.ERROR))

        # Navigate the main-panel stack: show config (or welcome) when idle/error,
        # show logs as soon as a launch begins.
        if state in (ServerState.IDLE, ServerState.ERROR):
            entry = self._ctrl.current_entry
            if entry:
                self._panel.show_config(entry, self._on_options_changed)
            else:
                self._panel.show_welcome()
        elif state == ServerState.LAUNCHING:
            self._panel.show_logs()
        elif state == ServerState.READY:
            # Wire the Send button and bench run button on first READY transition (idempotent).
            self._wire_tool_send()
            self._wire_bench_run()

    def _on_progress(self, fraction: float, label: str) -> None:
        """Update the progress bar.  fraction < 0 triggers an indeterminate pulse."""
        if fraction < 0:
            self._panel._progress_bar.pulse()
        else:
            self._panel.set_progress(fraction, label)

    def _on_substage(self, stepper: str, tour_left: str,
                     tour_right: str, dots: str) -> None:
        """Update stepper text, tour panel content, and dot indicator."""
        self._panel.set_stepper(stepper)
        self._panel.set_tour(tour_left, tour_right)
        self._panel._tour_dots.set_text(dots)

    def _on_catalog_loaded(self, catalog: ModelCatalog, compatible: list) -> None:
        """Populate the sidebar tree and device buttons when the catalog is ready."""
        self._sidebar.load_catalog(catalog, compatible)

    # ── User action handlers (called from Sidebar widgets) ────────────────────

    def _on_launch_clicked(self, entry: ModelEntry, port: str) -> None:
        """Collect current options from the config panel and ask the controller
        to start the server."""
        options = self._panel.get_options()
        self._ctrl.launch(entry, port, options)

    def _on_model_select(self, entry: ModelEntry) -> None:
        """Tell the controller a new model was selected and update the banner
        and config panel immediately so the user sees feedback."""
        self._ctrl.select_model(entry)
        self._panel._banner_info.set_text(
            f"{entry.display_name}  ·  {entry.device_type}"
            f"  ·  {entry.inference_engine}"
        )
        self._panel.show_config(entry, self._on_options_changed)

    def _on_options_changed(self, options) -> None:
        """Relay ConfigPanel option changes to the controller (e.g. for live
        command-preview updates or validation)."""
        self._ctrl.set_options(options)

    def _on_repo_change(self, path: Path) -> None:
        """Forward repo path changes to the controller so it can reload the
        model catalog."""
        self._ctrl.load_repo(path)

    def _on_close(self, win) -> bool:
        """Persist window dimensions and stop the server on close."""
        _settings.window_width  = self.get_width()
        _settings.window_height = self.get_height()
        _settings.save()
        if self._ctrl.state not in (ServerState.IDLE, ServerState.ERROR):
            self._ctrl.stop()
        return False

    # ── Tool-use tab helpers ──────────────────────────────────────────────────

    def _on_tool_result(self, rt) -> None:
        """Append a ToolRoundTrip step to the tools output buffer.

        Called via controller.on_tool_result which is dispatched through
        GLib.idle_add so this always runs on the GTK main thread.
        """
        self._panel.append_tool_result(rt)

    def _wire_tool_send(self) -> None:
        """Connect the Send button in the Tools page to send_tool_call().

        This method is idempotent — subsequent calls after the first READY
        transition are no-ops, guarded by _tool_send_wired.
        """
        if getattr(self, "_tool_send_wired", False):
            return
        self._tool_send_wired = True

        def _on_send(_btn):
            import json
            # Parse the tool definition from the editable JSON text view.
            try:
                tools = json.loads(
                    self._panel._tool_def_buf.get_text(
                        self._panel._tool_def_buf.get_start_iter(),
                        self._panel._tool_def_buf.get_end_iter(),
                        False,  # include_hidden_chars
                    )
                )
            except json.JSONDecodeError as e:
                self._panel.append_log(f"⚠ Invalid tool JSON: {e}")
                return
            prompt = self._panel._tool_prompt_entry.get_text().strip()
            if not prompt:
                return
            # Clear previous round-trip output before sending.
            self._panel._tool_output_buf.set_text("")
            # Show or hide the "tool use not enabled" hint based on current opts.
            opts = self._panel.get_options()
            if opts is not None:
                self._panel.set_tool_use_hint_visible(not opts.tool_use_enabled)
                if not opts.tool_use_enabled:
                    return
            self._ctrl.send_tool_call(tools, prompt)

        self._panel._tool_send_btn.connect("clicked", _on_send)

    # ── Bench tab helpers ─────────────────────────────────────────────────────

    def _on_bench_progress(self, line: str) -> None:
        """Forward a benchmark live-output line to the bench log buffer.

        Called via controller.on_bench_progress, dispatched through
        GLib.idle_add so this always runs on the GTK main thread.
        """
        self._panel.append_bench_progress(line)

    def _on_bench_result(self, result) -> None:
        """Append a completed BenchResult row to the bench results buffer.

        Called via controller.on_bench_result, dispatched through
        GLib.idle_add so this always runs on the GTK main thread.
        """
        self._panel.append_bench_result(result)

    def _wire_bench_run(self) -> None:
        """Connect the Run Benchmark button to run_benchmark() on the controller.

        This method is idempotent — subsequent calls after the first READY
        transition are no-ops, guarded by _bench_run_wired.
        """
        if getattr(self, "_bench_run_wired", False):
            return
        self._bench_run_wired = True

        def _on_run(_btn):
            mode = self._panel._bench_mode_combo.get_active_text() or "smoke-test"
            sweeps = self._panel._bench_sweeps_check.get_active()
            pct    = self._panel._bench_pct_check.get_active()
            # Clear previous live output and results before starting a new run.
            self._panel._bench_log_buf.set_text("")
            self._panel._bench_results_buf.set_text("")
            self._ctrl.run_benchmark(mode=mode, concurrency_sweeps=sweeps,
                                     percentile_report=pct)

        self._panel._bench_run_btn.connect("clicked", _on_run)
