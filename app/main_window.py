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
from gi.repository import Gdk, GLib, Gio, Gtk, Pango

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
    ServerState.RUNNING:       ("RUNNING",       "pill-loading"),
    ServerState.DONE:          ("DONE",          "pill-ready"),
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


def _format_param_count(param_count: Optional[float]) -> str:
    """Format param_count (raw float, e.g. 7e9) as a compact string like '7B', '335M'."""
    if param_count is None:
        return ""
    if param_count >= 1e12:
        return f"{param_count / 1e12:.0f}T"
    if param_count >= 1e9:
        v = param_count / 1e9
        return f"{v:.0f}B" if v >= 10 else f"{v:.1f}B"
    if param_count >= 1e6:
        v = param_count / 1e6
        return f"{v:.0f}M" if v >= 10 else f"{v:.1f}M"
    return ""


def _entry_label(entry, cached_repos: set) -> str:
    """Build model tree leaf label with optional size, ✓ (cached), and ⚠ (experimental) badges."""
    label = entry.display_name
    size = _format_param_count(getattr(entry, "param_count", None))
    if size:
        label += f"  {size}"
    if entry.hf_model_repo in cached_repos:
        label += "  ✓"
    if getattr(entry, "status", "") == "EXPERIMENTAL":
        label += "  ⚠"
    return label


class Sidebar(Gtk.Box):
    """Left sidebar: repo path picker, model tree, device toggles, port, launch/stop, HF status."""

    def __init__(self, on_launch, on_stop, on_model_select, on_device_select, on_repo_change,
                 on_reset=None, on_pull=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_size_request(290, -1)

        self._on_launch = on_launch
        self._on_stop = on_stop
        self._on_model_select = on_model_select
        self._on_device_select = on_device_select
        self._on_repo_change = on_repo_change
        self._on_reset = on_reset
        self._on_pull = on_pull

        self._catalog: Optional[ModelCatalog] = None
        self._selected_entry: Optional[ModelEntry] = None
        self._selected_device: Optional[str] = None
        self._device_buttons: dict = {}
        self._locked = False
        self._launch_connected_to_launch = True
        self._search_filter: str = ""
        self._cached_repos: set = set()
        self._compat_catalog = None      # set via set_compat_catalog()
        self.on_compat_select = None     # Callable[[CompatEntry], None]

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
        self._repo_entry.set_hexpand(True)
        repo_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        repo_row.append(self._repo_entry)
        self._pull_btn = Gtk.Button(label="↑")
        self._pull_btn.set_tooltip_text("git pull — update inference-server repo to latest")
        self._pull_btn.connect("clicked", lambda _: self._on_pull() if self._on_pull else None)
        repo_row.append(self._pull_btn)
        rbox.append(repo_row)
        self._git_info_label = Gtk.Label(label="")
        self._git_info_label.add_css_class("muted")
        self._git_info_label.set_halign(Gtk.Align.START)
        self._git_info_label.set_margin_start(2)
        rbox.append(self._git_info_label)
        self.append(rbox)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Model section label + search
        ml_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        ml_box.set_margin_start(8); ml_box.set_margin_end(8)
        ml_box.set_margin_top(6); ml_box.set_margin_bottom(2)
        ml_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        ml = Gtk.Label(label="MODEL"); ml.add_css_class("section-label")
        ml.set_halign(Gtk.Align.START); ml.set_hexpand(True)
        ml_header.append(ml)
        ml_box.append(ml_header)
        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Search models…")
        self._search_entry.connect("search-changed", self._on_search_changed)
        self._search_entry.connect("stop-search", lambda _: self._clear_search())
        ml_box.append(self._search_entry)
        self.append(ml_box)

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
        self._tree_view.set_has_tooltip(True)
        self._tree_view.connect("query-tooltip", self._on_tree_tooltip)
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
        self._port_entry.connect("changed", self._on_port_changed)
        pbox.append(self._port_entry)
        self._port_indicator = Gtk.Label(label="●")
        self._port_indicator.set_tooltip_text("Port availability")
        self._port_indicator.set_markup("<span foreground='#607D8B'>●</span>")
        pbox.append(self._port_indicator)
        self.append(pbox)
        self._port_check_timer: Optional[int] = None
        GLib.timeout_add(600, self._schedule_port_check)  # initial check after UI settles

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

        # Hardware section
        hw_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        hw_box.set_margin_start(8); hw_box.set_margin_end(8)
        hw_box.set_margin_top(4);   hw_box.set_margin_bottom(4)
        hw_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        hw_lbl = Gtk.Label(label="HARDWARE")
        hw_lbl.add_css_class("section-label")
        hw_lbl.set_halign(Gtk.Align.START)
        hw_lbl.set_hexpand(True)
        hw_header.append(hw_lbl)
        self._hw_refresh_btn = Gtk.Button(label="↻")
        self._hw_refresh_btn.add_css_class("flat")
        self._hw_refresh_btn.set_tooltip_text("Refresh chip telemetry (tt-smi -s)")
        self._hw_refresh_btn.connect("clicked", self._on_hw_refresh_clicked)
        hw_header.append(self._hw_refresh_btn)
        hw_box.append(hw_header)
        # Per-chip telemetry grid (hidden until data arrives)
        self._chip_grid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self._chip_grid.set_visible(False)
        hw_box.append(self._chip_grid)
        self._reset_btn = Gtk.Button(label="↺  Reset  (tt-smi -r)")
        self._reset_btn.add_css_class("destructive-action")
        self._reset_btn.set_hexpand(True)
        self._reset_btn.set_tooltip_text(
            "Reset all TT devices. Required when switching between model families "
            "(e.g. LLM → video)."
        )
        self._reset_btn.connect("clicked", self._on_reset_clicked)
        hw_box.append(self._reset_btn)
        self.append(hw_box)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # HF token status
        self._hf_label = Gtk.Label()
        self._hf_label.set_margin_start(8); self._hf_label.set_margin_top(4); self._hf_label.set_margin_bottom(6)
        self._hf_label.set_halign(Gtk.Align.START)
        self.append(self._hf_label)
        self._update_hf_status()

    def _on_port_changed(self, entry: Gtk.Entry) -> None:
        self._save_port()
        self._schedule_port_check()

    def _save_port(self):
        _settings.last_port = self._port_entry.get_text()
        _settings.save()

    def _schedule_port_check(self) -> bool:
        """Debounce: cancel any pending check and schedule a new one in 400ms."""
        if self._port_check_timer is not None:
            GLib.source_remove(self._port_check_timer)
        self._port_check_timer = GLib.timeout_add(400, self._run_port_check)
        return False  # one-shot when called from timeout_add

    def _run_port_check(self) -> bool:
        """Kick off a background socket check for the configured port."""
        import socket
        import threading
        self._port_check_timer = None
        port_str = self._port_entry.get_text().strip()

        def _check():
            try:
                port = int(port_str)
            except ValueError:
                GLib.idle_add(self._set_port_indicator, None)
                return
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    GLib.idle_add(self._set_port_indicator, True)
            except OSError:
                GLib.idle_add(self._set_port_indicator, False)

        threading.Thread(target=_check, daemon=True).start()
        return False  # one-shot

    def _set_port_indicator(self, in_use: Optional[bool]) -> None:
        """Update port indicator: None=unknown, False=free, True=in use."""
        if in_use is None:
            self._port_indicator.set_markup("<span foreground='#607D8B'>●</span>")
            self._port_indicator.set_tooltip_text("Invalid port")
        elif in_use:
            self._port_indicator.set_markup("<span foreground='#FF6B6B'>●</span>")
            self._port_indicator.set_tooltip_text("Port in use")
        else:
            self._port_indicator.set_markup("<span foreground='#27AE60'>●</span>")
            self._port_indicator.set_tooltip_text("Port free")

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
        self._scan_hf_cache_async()

    def set_compat_catalog(self, catalog) -> None:
        """Attach the compatibility catalog for DISCOVER results when searching."""
        self._compat_catalog = catalog
        # If there's an active search, refresh the tree to show DISCOVER results.
        if self._search_filter:
            if self._selected_device:
                self._rebuild_tree([self._selected_device])
            else:
                self._rebuild_tree(None)

    def _scan_hf_cache_async(self) -> None:
        """Background scan of HF cache — updates tree with ✓ badges on completion."""
        import threading
        from hf_cache import scan_all_cached
        if not self._catalog:
            return
        all_repos = {e.hf_model_repo for e in self._catalog.all_entries()}

        def _run():
            cached = scan_all_cached(all_repos)
            GLib.idle_add(self.set_cached_repos, cached)

        threading.Thread(target=_run, daemon=True).start()

    def set_cached_repos(self, repos: set) -> None:
        """Called on GTK thread after background HF cache scan completes."""
        self._cached_repos = repos
        if self._selected_device:
            self._rebuild_tree([self._selected_device])
        else:
            self._rebuild_tree(None)

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

    def _on_search_changed(self, entry: "Gtk.SearchEntry") -> None:
        self._search_filter = entry.get_text().strip().lower()
        if self._selected_device:
            self._rebuild_tree([self._selected_device])
        else:
            self._rebuild_tree(None)

    def _clear_search(self) -> None:
        self._search_entry.set_text("")
        self._search_filter = ""
        if self._selected_device:
            self._rebuild_tree([self._selected_device])
        else:
            self._rebuild_tree(None)

    def _rebuild_tree(self, filter_devices: Optional[List[str]]):
        self._tree_store.clear()
        if not self._catalog:
            return
        cat = self._catalog.get_compatible(filter_devices) if filter_devices else self._catalog
        tree = cat.get_tree()
        search = getattr(self, "_search_filter", "")
        expanded = _settings.tree_expanded_types or ["LLM"]
        last_model = _settings.last_model
        searching = bool(search)

        # STARRED section — pinned models always visible at top
        if not searching:
            starred = _settings.starred_models or []
            starred_entries = []
            for rec in starred:
                entry = self._catalog.get_entry(rec.get("model_name", ""), rec.get("device", ""))
                if entry:
                    starred_entries.append(entry)
            if starred_entries:
                star_it = self._tree_store.append(
                    None, [f"★ STARRED ({len(starred_entries)})", "", "", False]
                )
                for entry in starred_entries:
                    label = "★ " + _entry_label(entry, self._cached_repos)
                    leaf_it = self._tree_store.append(
                        star_it, [label, entry.model_name, entry.device_type, True]
                    )
                    if entry.model_name == last_model:
                        self._tree_view.get_selection().select_iter(leaf_it)
                self._tree_view.expand_row(self._tree_store.get_path(star_it), False)

        # RECENT section — show up to 3 most-recently-launched models at top
        if not searching:
            recents = _settings.recent_models or []
            recent_entries = []
            for rec in recents[:3]:
                entry = self._catalog.get_entry(rec.get("model_name", ""), rec.get("device", ""))
                if entry:
                    recent_entries.append(entry)
            if recent_entries:
                rec_it = self._tree_store.append(
                    None, [f"RECENT ({len(recent_entries)})", "", "", False]
                )
                for entry in recent_entries:
                    label = _entry_label(entry, self._cached_repos)
                    leaf_it = self._tree_store.append(
                        rec_it, [label, entry.model_name, entry.device_type, True]
                    )
                    if entry.model_name == last_model:
                        self._tree_view.get_selection().select_iter(leaf_it)
                self._tree_view.expand_row(self._tree_store.get_path(rec_it), False)

        for type_name in _TYPE_ORDER:
            if type_name not in tree:
                continue
            families = tree[type_name]
            # When searching, pre-filter entries so we can count visible leaves.
            if searching:
                filtered_families = {
                    fam: [e for e in entries if search in e.display_name.lower()
                          or search in fam.lower()]
                    for fam, entries in families.items()
                }
                filtered_families = {f: e for f, e in filtered_families.items() if e}
                if not filtered_families:
                    continue
                total = sum(len(v) for v in filtered_families.values())
            else:
                filtered_families = families
                total = sum(len(v) for v in families.values())

            type_it = self._tree_store.append(
                None, [f"{_TYPE_LABEL.get(type_name, type_name)} ({total})", "", "", False]
            )
            for family, entries in sorted(filtered_families.items()):
                fam_it = self._tree_store.append(type_it, [family, "", "", False])
                for entry in entries:
                    label = _entry_label(entry, self._cached_repos)
                    leaf_it = self._tree_store.append(
                        fam_it, [label, entry.model_name, entry.device_type, True]
                    )
                    if entry.model_name == last_model:
                        self._tree_view.get_selection().select_iter(leaf_it)
            if searching or type_name in expanded:
                self._tree_view.expand_row(self._tree_store.get_path(type_it), True)

        # DISCOVER section — compat catalog entries not in model_spec, shown when searching.
        if searching and self._compat_catalog:
            self._append_discover_results(search)

    def _append_discover_results(self, search: str) -> None:
        """Add DISCOVER tree section from compat catalog for search query."""
        from compat_catalog import _HW_MAP
        # Build the set of display names already shown by model_spec catalog.
        known_names: set = set()
        if self._catalog:
            known_names = {e.display_name.lower() for e in self._catalog.all_entries()}

        # When a device is selected, map its ID back to catalog hardware names.
        active_hw: set = set()
        if self._selected_device:
            dt = self._selected_device
            active_hw = {hw for hw, mapped in _HW_MAP.items() if mapped == dt}
            active_hw.add(dt.lower())  # also accept the device ID itself

        # Gather matching compat entries not already in model_spec.
        sw_buckets: dict = {}  # software_stack → [CompatEntry]
        for entry in self._compat_catalog.all_entries():
            if entry.display_name.lower() in known_names:
                continue
            name_match = search in entry.display_name.lower() or search in entry.id.lower()
            desc_match = search in (entry.model_description or "").lower()
            fam_match  = search in (entry.family or "").lower()
            if not (name_match or desc_match or fam_match):
                continue
            # Determine applicable software stacks from all compatibility records.
            for compat in entry.compatibility:
                if compat.status == "Not Supported":
                    continue
                # Filter by selected device if one is active.
                if active_hw and compat.hardware.lower() not in active_hw:
                    continue
                for sw in compat.software:
                    if sw not in sw_buckets:
                        sw_buckets[sw] = []
                    if entry not in sw_buckets[sw]:
                        sw_buckets[sw].append(entry)

        if not sw_buckets:
            return

        total = len({e.id for entries in sw_buckets.values() for e in entries})
        disc_it = self._tree_store.append(
            None, [f"DISCOVER ({total} via compat catalog)", "", "", False]
        )
        for sw, entries in sorted(sw_buckets.items()):
            sw_it = self._tree_store.append(disc_it, [sw, "", "", False])
            for entry in entries[:15]:
                size_str = f"  {entry.model_size}" if entry.model_size else ""
                label = f"{entry.display_name}{size_str}"
                self._tree_store.append(
                    sw_it, [label, f"__compat__:{entry.id}", sw, True]
                )
        self._tree_view.expand_row(self._tree_store.get_path(disc_it), True)

    def _on_tree_selection(self, sel):
        model, it = sel.get_selected()
        if it is None:
            return
        if not model.get_value(it, 3):  # not a leaf
            return
        model_key = model.get_value(it, 1)
        device = model.get_value(it, 2)

        # DISCOVER entry — model_key has a __compat__: prefix.
        if model_key.startswith("__compat__:"):
            entry_id = model_key[len("__compat__:"):]
            if self._compat_catalog and self.on_compat_select:
                compat_entry = self._compat_catalog.lookup(entry_id)
                if compat_entry:
                    self.on_compat_select(compat_entry)
            return

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

    def _on_tree_tooltip(self, widget, x, y, keyboard_mode, tooltip) -> bool:
        """Show a rich tooltip when hovering over a model row."""
        bx, by = widget.convert_widget_to_bin_window_coords(x, y)
        result = widget.get_path_at_pos(bx, by)
        if result is None:
            return False
        path, _col, _cx, _cy = result
        it = self._tree_store.get_iter(path)
        if it is None or not self._tree_store.get_value(it, 3):  # only leaf rows
            return False
        model_key = self._tree_store.get_value(it, 1)
        device    = self._tree_store.get_value(it, 2)

        if model_key.startswith("__compat__:"):
            # Compat catalog entry.
            entry_id = model_key[len("__compat__:"):]
            if not self._compat_catalog:
                return False
            ce = self._compat_catalog.lookup(entry_id)
            if not ce:
                return False
            stacks = sorted({sw for c in ce.compatibility for sw in c.software
                             if c.status != "Not Supported"})
            hw_names = sorted({c.hardware for c in ce.compatibility
                               if c.status != "Not Supported"})
            parts = [f"<b>{ce.display_name}</b>"]
            if ce.model_description:
                parts.append(ce.model_description[:200])
            if ce.model_size:
                parts.append(f"Size: {ce.model_size}")
            if stacks:
                parts.append(f"Software: {', '.join(stacks)}")
            if hw_names:
                parts.append(f"Hardware: {', '.join(hw_names[:6])}")
            tooltip.set_markup("\n".join(parts))
            widget.set_tooltip_row(tooltip, path)
            return True

        # Regular model_spec entry.
        if not self._catalog:
            return False
        entry = self._catalog.get_entry(model_key, device)
        if not entry:
            return False
        parts = [f"<b>{entry.display_name}</b>"]
        if hasattr(entry, "model_description") and entry.model_description:
            parts.append(entry.model_description[:200])
        if entry.device_type:
            parts.append(f"Device: {entry.device_type}")
        if hasattr(entry, "param_count") and entry.param_count:
            parts.append(f"Params: {_format_param_count(entry.param_count)}")
        if hasattr(entry, "status") and entry.status:
            parts.append(f"Status: {entry.status}")
        tooltip.set_markup("\n".join(parts))
        widget.set_tooltip_row(tooltip, path)
        return True

    def set_locked(self, locked: bool):
        self._locked = locked
        self._tree_view.set_sensitive(not locked)
        self._search_entry.set_sensitive(not locked)
        self._repo_entry.set_sensitive(not locked)
        for btn in self._device_buttons.values():
            btn.set_sensitive(not locked)
        self._port_entry.set_sensitive(not locked)
        self._reset_btn.set_sensitive(not locked)
        self._pull_btn.set_sensitive(not locked)

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

    def _on_reset_clicked(self, btn):
        if self._on_reset:
            self._on_reset()

    def _on_hw_refresh_clicked(self, _btn):
        pass   # MainWindow wires this to ctrl.refresh_hardware_status

    def update_hardware_status(self, chips: list) -> None:
        """Populate the per-chip telemetry grid from a List[ChipStatus]."""
        while child := self._chip_grid.get_first_child():
            self._chip_grid.remove(child)
        for chip in chips:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            row.set_margin_start(2)
            bt = (chip.board_type or "?").upper()
            id_lbl = Gtk.Label(label=f"#{chip.index} {bt}")
            id_lbl.set_width_chars(10)
            id_lbl.set_xalign(0.0)
            id_lbl.add_css_class("muted")
            row.append(id_lbl)
            if chip.temp_c is not None:
                t = chip.temp_c
                temp_lbl = Gtk.Label(label=f"{t:.0f}°C")
                temp_lbl.set_width_chars(6)
                temp_lbl.set_xalign(0.0)
                if t >= 80:
                    temp_lbl.add_css_class("temp-hot")
                elif t >= 65:
                    temp_lbl.add_css_class("temp-warm")
                else:
                    temp_lbl.add_css_class("temp-ok")
                row.append(temp_lbl)
            if chip.aiclk_mhz is not None:
                clk_lbl = Gtk.Label(label=f"{chip.aiclk_mhz}MHz")
                clk_lbl.add_css_class("muted")
                clk_lbl.set_width_chars(8)
                clk_lbl.set_xalign(0.0)
                row.append(clk_lbl)
            if chip.fw_version:
                fw_lbl = Gtk.Label(label=chip.fw_version)
                fw_lbl.add_css_class("muted")
                fw_lbl.set_ellipsize(Pango.EllipsizeMode.END)
                row.append(fw_lbl)
            self._chip_grid.append(row)
        self._chip_grid.set_visible(bool(chips))

    def refresh_git_info(self, branch: str, sha: str) -> None:
        """Update the git branch/commit label below the repo entry."""
        if branch or sha:
            self._git_info_label.set_text(f"  {branch}  @{sha}" if branch else f"  @{sha}")
        else:
            self._git_info_label.set_text("")

    def get_selected_entry(self) -> Optional[ModelEntry]:
        return self._selected_entry

    def get_selected_device(self) -> Optional[str]:
        return self._selected_device

    def get_port(self) -> str:
        return self._port_entry.get_text() or "8000"

    def select_model_by_id(self, model_key: str) -> None:
        """Find model_key in the tree, select it, and scroll it into view."""
        def _find_leaf(store, parent=None):
            it = store.iter_children(parent)
            while it:
                if store.get_value(it, 3):   # is_leaf
                    if store.get_value(it, 1) == model_key:
                        return it
                else:
                    found = _find_leaf(store, it)
                    if found:
                        return found
                it = store.iter_next(it)
            return None

        leaf_it = _find_leaf(self._tree_store)
        if leaf_it is None:
            return
        path = self._tree_store.get_path(leaf_it)
        self._tree_view.expand_to_path(path)
        self._tree_view.get_selection().select_iter(leaf_it)
        self._tree_view.scroll_to_cell(path, None, False, 0.0, 0.0)


_LOG_LEVELS_ORDERED = ["DEBUG", "INFO", "WARN", "ERROR"]
_MAX_LOG_ENTRIES = 5000


class AdUnit(Gtk.Box):
    """Rotating 'Did you know?' card at the bottom of the main panel.

    Auto-advances every 8 s.
    - Click the card body to pause; click again (outside the rail link) to advance + resume.
    - Cards with a model_id show a '→ Rail' button that selects the model in the sidebar.
    - '›' / '▶' button always advances (and resumes if paused).
    """
    _INTERVAL_MS = 8000

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("ad-unit")
        self.set_margin_bottom(12)
        self._cards: list = []
        self._idx: int = 0
        self._timer: Optional[int] = None
        self._paused: bool = False
        self._current_model_id: Optional[str] = None
        self._on_select_model: Optional[callable] = None
        self._build()

    def _build(self):
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        inner.set_margin_start(10); inner.set_margin_end(10)
        inner.set_margin_top(6);    inner.set_margin_bottom(8)

        self._tag = Gtk.Label(label="")
        self._tag.add_css_class("pill")
        self._tag.add_css_class("pill-idle")
        self._tag.set_valign(Gtk.Align.START)
        inner.append(self._tag)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True)
        self._headline = Gtk.Label()
        self._headline.set_halign(Gtk.Align.START)
        self._headline.set_markup("<b>Loading…</b>")
        text_box.append(self._headline)
        self._body = Gtk.Label(label="")
        self._body.add_css_class("muted")
        self._body.set_halign(Gtk.Align.START)
        self._body.set_wrap(True)
        self._body.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        text_box.append(self._body)
        inner.append(text_box)

        # Rail-link button — visible only for cards with a model_id
        self._find_btn = Gtk.Button(label="→ Rail")
        self._find_btn.add_css_class("flat")
        self._find_btn.set_valign(Gtk.Align.CENTER)
        self._find_btn.set_visible(False)
        self._find_btn.set_tooltip_text("Select this model in the sidebar")
        self._find_btn.connect("clicked", self._on_find_clicked)
        inner.append(self._find_btn)

        # Advance button — shows ▶ when paused, › when playing
        self._adv_btn = Gtk.Button(label="›")
        self._adv_btn.add_css_class("flat")
        self._adv_btn.set_valign(Gtk.Align.CENTER)
        self._adv_btn.connect("clicked", lambda _: self._advance())
        inner.append(self._adv_btn)

        # Click on body (labels / empty space) pauses; second click advances + resumes.
        # Button clicks are claimed by the buttons themselves and do NOT fire this gesture.
        gesture = Gtk.GestureClick.new()
        gesture.connect("pressed", self._on_body_pressed)
        inner.add_controller(gesture)

        self.append(inner)

    def set_on_select_model(self, callback: Optional[callable]) -> None:
        self._on_select_model = callback
        self._find_btn.set_visible(
            bool(self._current_model_id and self._on_select_model)
        )

    def set_cards(self, cards: list) -> None:
        """Replace the card pool and show the first card immediately."""
        self._cards = cards or []
        self._idx = 0
        self._paused = False
        self._adv_btn.set_label("›")
        self._show_current()
        self._restart_timer()

    def _show_current(self) -> None:
        if not self._cards:
            self._headline.set_markup("<b>Did you know?</b>")
            self._body.set_text("Loading recommendations…")
            self._tag.set_text("")
            self._current_model_id = None
            self._find_btn.set_visible(False)
            return
        card = self._cards[self._idx % len(self._cards)]
        headline = card.get("headline", "")
        # Escape Pango markup special characters in the headline text
        headline_esc = headline.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self._headline.set_markup(f"<b>{headline_esc}</b>")
        self._body.set_text(card.get("body", ""))
        self._tag.set_text(card.get("tag", ""))
        self._current_model_id = card.get("model_id")
        self._find_btn.set_visible(
            bool(self._current_model_id and self._on_select_model)
        )

    def _advance(self) -> None:
        if self._cards:
            self._idx = (self._idx + 1) % len(self._cards)
        self._paused = False
        self._adv_btn.set_label("›")
        self._show_current()
        self._restart_timer()

    def _restart_timer(self) -> None:
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None
        if not self._paused:
            self._timer = GLib.timeout_add(self._INTERVAL_MS, self._on_timer)

    def _on_timer(self) -> bool:
        self._advance()
        return False  # one-shot; _advance restarts it

    def _on_body_pressed(self, gesture, n_press, x, y) -> None:
        if not self._paused:
            # First click: pause — stop the timer, signal paused state via button label
            self._paused = True
            if self._timer is not None:
                GLib.source_remove(self._timer)
                self._timer = None
            self._adv_btn.set_label("▶")
        else:
            # Second click: advance to next card + resume auto-advance
            self._advance()

    def _on_find_clicked(self, _btn) -> None:
        if self._current_model_id and self._on_select_model:
            self._on_select_model(self._current_model_id)


class MainPanel(Gtk.Box):
    """Right panel: status banner, sub-stage stepper, progress bar, tour panel, log view."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._auto_scroll = True
        self._pulse_source: Optional[int] = None
        self._log_entries: list = []        # (line_text, level_str, ts_float) tuples
        self._hidden_levels: set = set()
        self._log_search_filter: str = ""
        self._show_timestamps: bool = False
        self._uptime_start: Optional[float] = None
        self._uptime_timer: Optional[int] = None
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

        # Uptime counter — visible when READY
        self._uptime_label = Gtk.Label(label="")
        self._uptime_label.add_css_class("muted")
        self._uptime_label.set_visible(False)
        banner.append(self._uptime_label)

        # "Restart" button — visible when READY or ERROR (and previous entry exists)
        self._restart_btn = Gtk.Button(label="↺")
        self._restart_btn.add_css_class("flat")
        self._restart_btn.set_tooltip_text("Restart server with same model and options")
        self._restart_btn.set_visible(False)
        banner.append(self._restart_btn)

        # "Copy curl" button — only visible when READY
        self._copy_curl_btn = Gtk.Button(label="⧉ curl")
        self._copy_curl_btn.add_css_class("flat")
        self._copy_curl_btn.set_tooltip_text("Copy a test curl command to clipboard")
        self._copy_curl_btn.set_visible(False)
        self._copy_curl_btn.connect("clicked", self._on_copy_curl)
        banner.append(self._copy_curl_btn)

        # "Open API" button — opens http://localhost:{port}/docs in default browser
        self._open_api_btn = Gtk.Button(label="⤤ API")
        self._open_api_btn.add_css_class("flat")
        self._open_api_btn.set_tooltip_text("Open API explorer in default browser")
        self._open_api_btn.set_visible(False)
        self._open_api_btn.connect("clicked", self._on_open_api)
        banner.append(self._open_api_btn)

        # Star/unstar toggle — visible when a model is selected
        self._star_btn = Gtk.Button(label="☆")
        self._star_btn.add_css_class("flat")
        self._star_btn.set_tooltip_text("Pin/unpin this model to Starred")
        self._star_btn.set_visible(False)
        banner.append(self._star_btn)

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
        welcome_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        welcome_box.set_valign(Gtk.Align.CENTER)
        welcome_box.set_halign(Gtk.Align.CENTER)
        welcome_box.set_margin_start(40); welcome_box.set_margin_end(40)
        self._welcome_primary = Gtk.Label(label="Select a model to configure and launch")
        self._welcome_primary.add_css_class("muted")
        welcome_box.append(self._welcome_primary)
        self._welcome_setup = Gtk.Label()
        self._welcome_setup.set_markup(
            "<b>Getting started</b>\n\n"
            "1. Clone the inference server repo:\n"
            "   <tt>git clone https://github.com/tenstorrent/tt-inference-server\n"
            "   ~/code/tt-inference-server</tt>\n\n"
            "2. Set the path above in the ⚙ repo field, or it will be found automatically.\n\n"
            "3. Set your HuggingFace token:\n"
            "   <tt>export HF_TOKEN=hf_...</tt>\n\n"
            "4. Select a model from the sidebar and click Launch."
        )
        self._welcome_setup.set_justify(Gtk.Justification.LEFT)
        self._welcome_setup.set_halign(Gtk.Align.START)
        self._welcome_setup.set_visible(False)
        welcome_box.append(self._welcome_setup)
        self._stack.add_named(welcome_box, "welcome")

        # ── Discover page ─────────────────────────────────────────────────────
        # Shown when the user selects a compat-catalog-only (tt-forge/tt-metal) entry.
        disc_scroll = Gtk.ScrolledWindow()
        disc_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        disc_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        disc_box.set_margin_start(16); disc_box.set_margin_end(16)
        disc_box.set_margin_top(12);   disc_box.set_margin_bottom(12)

        self._disc_name_lbl = Gtk.Label(label="")
        self._disc_name_lbl.set_halign(Gtk.Align.START)
        self._disc_name_lbl.set_markup("<b>Model</b>")
        disc_box.append(self._disc_name_lbl)

        self._disc_tags_lbl = Gtk.Label(label="")
        self._disc_tags_lbl.add_css_class("muted")
        self._disc_tags_lbl.set_halign(Gtk.Align.START)
        self._disc_tags_lbl.set_wrap(True)
        disc_box.append(self._disc_tags_lbl)

        disc_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        self._disc_desc_lbl = Gtk.Label(label="")
        self._disc_desc_lbl.set_halign(Gtk.Align.START)
        self._disc_desc_lbl.set_wrap(True)
        self._disc_desc_lbl.set_visible(False)
        disc_box.append(self._disc_desc_lbl)

        compat_lbl = Gtk.Label(label="COMPATIBLE HARDWARE")
        compat_lbl.add_css_class("muted")
        compat_lbl.set_halign(Gtk.Align.START)
        disc_box.append(compat_lbl)
        self._disc_compat_buf = Gtk.TextBuffer()
        self._disc_compat_view = Gtk.TextView(buffer=self._disc_compat_buf)
        self._disc_compat_view.set_editable(False)
        self._disc_compat_view.set_monospace(True)
        self._disc_compat_view.add_css_class("log-view")
        self._disc_compat_view.set_size_request(-1, 100)
        disc_box.append(self._disc_compat_view)

        self._disc_run_btn = Gtk.Button(label="▶ Run via Developer Image")
        self._disc_run_btn.add_css_class("suggested-action")
        self._disc_run_btn.set_halign(Gtk.Align.START)
        self._disc_run_btn.set_visible(False)
        self._disc_run_btn.connect("clicked", self._on_disc_run_clicked)
        disc_box.append(self._disc_run_btn)

        self._disc_hint_lbl = Gtk.Label(label="")
        self._disc_hint_lbl.add_css_class("muted")
        self._disc_hint_lbl.set_halign(Gtk.Align.START)
        self._disc_hint_lbl.set_wrap(True)
        self._disc_hint_lbl.set_visible(False)
        disc_box.append(self._disc_hint_lbl)

        disc_scroll.set_child(disc_box)
        self._stack.add_named(disc_scroll, "discover")
        self._disc_current_entry = None   # currently displayed CompatEntry
        self._disc_run_callback = None    # set by caller

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
        self._jump_error_btn = Gtk.Button(label="↓ Error")
        self._jump_error_btn.add_css_class("flat")
        self._jump_error_btn.set_tooltip_text("Jump to last ERROR line in log")
        self._jump_error_btn.set_visible(False)
        self._jump_error_btn.connect("clicked", self._on_jump_to_error)
        filter_bar.append(self._jump_error_btn)
        self._save_log_btn = Gtk.Button(label="⬇ Save")
        self._save_log_btn.add_css_class("flat")
        self._save_log_btn.set_tooltip_text("Save log to file")
        self._save_log_btn.connect("clicked", self._on_save_log)
        filter_bar.append(self._save_log_btn)

        self._copy_log_btn = Gtk.Button(label="⎘ Copy")
        self._copy_log_btn.add_css_class("flat")
        self._copy_log_btn.set_tooltip_text("Copy selected text, or all visible log lines (Ctrl+A then Ctrl+C)")
        self._copy_log_btn.connect("clicked", self._on_copy_log)
        filter_bar.append(self._copy_log_btn)

        self._ts_btn = Gtk.ToggleButton(label="🕐")
        self._ts_btn.add_css_class("flat")
        self._ts_btn.set_tooltip_text("Toggle timestamps on log lines")
        self._ts_btn.connect("toggled", self._on_ts_toggled)
        filter_bar.append(self._ts_btn)

        # Log search field — filters displayed lines to those matching the query.
        self._log_search = Gtk.SearchEntry()
        self._log_search.set_placeholder_text("Search logs…")
        self._log_search.set_max_width_chars(20)
        self._log_search.connect("search-changed", self._on_log_search_changed)
        filter_bar.append(self._log_search)

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

        # Right-click context menu for copy actions.
        self._log_ctx_popover = self._build_log_context_popover()
        self._log_ctx_popover.set_parent(self._log_view)
        _rclick = Gtk.GestureClick(button=3)
        _rclick.connect("pressed", self._on_log_right_click)
        self._log_view.add_controller(_rclick)

        # Ctrl+C key handler — copies selection or all visible text.
        _key_ctrl = Gtk.EventControllerKey()
        _key_ctrl.connect("key-pressed", self._on_log_key_pressed)
        self._log_view.add_controller(_key_ctrl)

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

        # History section — persisted results across sessions
        hist_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hist_header.set_margin_top(8)
        hist_lbl = Gtk.Label(label="HISTORY")
        hist_lbl.add_css_class("muted")
        hist_lbl.set_halign(Gtk.Align.START)
        hist_lbl.set_hexpand(True)
        hist_header.append(hist_lbl)
        self._bench_csv_btn = Gtk.Button(label="⬇ CSV")
        self._bench_csv_btn.add_css_class("flat")
        self._bench_csv_btn.set_tooltip_text("Export benchmark history as CSV")
        self._bench_csv_btn.connect("clicked", self._on_bench_export_csv)
        hist_header.append(self._bench_csv_btn)
        self._bench_clear_btn = Gtk.Button(label="✕ Clear")
        self._bench_clear_btn.add_css_class("flat")
        self._bench_clear_btn.set_tooltip_text("Clear benchmark history")
        self._bench_clear_btn.connect("clicked", self._on_bench_clear_history)
        hist_header.append(self._bench_clear_btn)
        bench_box.append(hist_header)
        self._bench_history_buf = Gtk.TextBuffer()
        bench_hist_view = Gtk.TextView(buffer=self._bench_history_buf)
        bench_hist_view.set_editable(False)
        bench_hist_view.set_monospace(True)
        bench_hist_view.add_css_class("log-view")
        bench_hist_scroll = Gtk.ScrolledWindow()
        bench_hist_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        bench_hist_scroll.set_vexpand(True)
        bench_hist_scroll.set_child(bench_hist_view)
        bench_box.append(bench_hist_scroll)

        self._stack.add_named(bench_box, "bench")

        self.append(self._stack)

        # Always-on ad unit at the bottom of the main panel
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        self._ad_unit = AdUnit()
        self.append(self._ad_unit)

    # ---------------------------------------------------------------- stack navigation

    def show_welcome(self, setup_guide: bool = False) -> None:
        """Switch to the welcome splash. Pass setup_guide=True to show first-run instructions."""
        self._welcome_primary.set_visible(not setup_guide)
        self._welcome_setup.set_visible(setup_guide)
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

    def show_discover(self, compat_entry, on_run=None) -> None:
        """Show the DISCOVER page for a compat-catalog entry."""
        self._disc_current_entry = compat_entry
        self._disc_run_callback = on_run

        self._disc_name_lbl.set_markup(f"<b>{compat_entry.display_name}</b>")

        tags = []
        if compat_entry.family:
            tags.append(compat_entry.family)
        tags += compat_entry.tasks or []
        if compat_entry.model_size:
            tags.append(compat_entry.model_size)
        self._disc_tags_lbl.set_text("  ·  ".join(tags) if tags else "")

        desc = compat_entry.model_description or ""
        self._disc_desc_lbl.set_text(desc)
        self._disc_desc_lbl.set_visible(bool(desc))

        # Build compatibility table.
        lines = []
        sw_stacks: set = set()
        for c in compat_entry.compatibility:
            status_icon = {"Supported": "✓", "Experimental": "⚡"}.get(c.status, "✗")
            sw_str = ", ".join(c.software) if c.software else "?"
            lines.append(f"  {status_icon}  {c.hardware:12s}  {c.chip_set:12s}  {sw_str}")
            sw_stacks.update(c.software)
        self._disc_compat_buf.set_text("\n".join(lines))

        # Show run button for tt-forge / tt-metal if applicable.
        can_run = bool(sw_stacks & {"tt-forge", "tt-metal"})
        if can_run:
            primary_sw = "tt-forge" if "tt-forge" in sw_stacks else "tt-metal"
            self._disc_run_btn.set_label(f"▶ Run via Developer Image  ({primary_sw})")
            self._disc_run_btn.set_visible(True)
            self._disc_hint_lbl.set_visible(False)
        else:
            self._disc_run_btn.set_visible(False)
            self._disc_hint_lbl.set_text(
                "This model is only available via tt-inference-server. "
                "It may appear in the model list once the server repo is configured."
            )
            self._disc_hint_lbl.set_visible(True)

        self._stack.set_visible_child_name("discover")

    def _on_disc_run_clicked(self, _btn) -> None:
        entry = self._disc_current_entry
        if entry and self._disc_run_callback:
            # Determine the primary software stack.
            sw_stacks = {sw for c in entry.compatibility for sw in c.software
                         if c.status != "Not Supported"}
            primary_sw = "tt-forge" if "tt-forge" in sw_stacks else next(iter(sw_stacks), "tt-forge")
            self._disc_run_callback(entry.id, primary_sw)

    def get_options(self):
        """Return the current LaunchOptions from ConfigPanel, or None if not yet created."""
        if self._config_panel is not None:
            return self._config_panel.get_options()
        return None

    def set_compat_info(self, compat_entry) -> None:
        """Forward compat catalog entry to the config panel if it exists."""
        if self._config_panel is not None:
            self._config_panel.set_compat_info(compat_entry)

    def set_dev_launch_callback(self, cb) -> None:
        """Set the on_dev_launch callback on the config panel."""
        if self._config_panel is not None:
            self._config_panel.on_dev_launch = cb

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

    def _format_ts(self, ts: float) -> str:
        import datetime
        return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")

    def _insert_line_to_buffer(self, line: str, level: str, ts: float = 0.0):
        import time as _time
        buf = self._log_buf
        end = buf.get_end_iter()
        if buf.get_char_count() > 0:
            buf.insert(end, "\n")
            end = buf.get_end_iter()
        start_off = end.get_offset()
        if self._show_timestamps and ts:
            ts_str = self._format_ts(ts) + "  "
            buf.insert_with_tags_by_name(end, ts_str, "ts")
            end = buf.get_end_iter()
            start_off = end.get_offset()
        buf.insert(end, line)
        tag_name = f"lvl_{level}" if level else None
        if tag_name:
            s = buf.get_iter_at_offset(start_off)
            buf.apply_tag_by_name(tag_name, s, buf.get_end_iter())

    def append_log(self, line: str):
        import time as _time
        level = self._detect_level(line)
        ts = _time.time()
        # Store (capped at _MAX_LOG_ENTRIES)
        self._log_entries.append((line, level, ts))
        if len(self._log_entries) > _MAX_LOG_ENTRIES:
            self._log_entries = self._log_entries[-_MAX_LOG_ENTRIES:]
        # Only insert if this level isn't hidden and matches any active search filter
        if self._line_visible(line, level):
            self._insert_line_to_buffer(line, level, ts)
        self._update_log_count()
        # Show jump-to-error button when an ERROR/CRITICAL line lands
        if level == "ERROR" and not self._jump_error_btn.get_visible():
            self._jump_error_btn.set_visible(True)

    def _on_jump_to_error(self, _btn) -> None:
        """Scroll the log view to the last ERROR-level line in the log entries."""
        # Find the index of the last error entry
        last_err_idx = None
        for i in range(len(self._log_entries) - 1, -1, -1):
            _, lvl, _ts = self._log_entries[i]
            if lvl == "ERROR":
                last_err_idx = i
                break
        if last_err_idx is None:
            return
        # Count visible lines up to that index to find buffer position
        visible_line = 0
        for i, (_, lvl, _ts) in enumerate(self._log_entries):
            if lvl not in self._hidden_levels:
                if i == last_err_idx:
                    break
                visible_line += 1
        buf = self._log_buf
        it = buf.get_iter_at_line(visible_line)
        self._log_view.scroll_to_iter(it, 0.1, True, 0.0, 0.3)

    def _on_save_log(self, _btn) -> None:
        """Open a file-save dialog and write the current log to disk."""
        import datetime
        dialog = Gtk.FileDialog()
        dialog.set_title("Save log")
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dialog.set_initial_name(f"tt-runner-log-{ts}.txt")
        dialog.save(self.get_root(), None, self._on_save_log_done, None)

    def _on_save_log_done(self, dialog, result, _data) -> None:
        try:
            gfile = dialog.save_finish(result)
        except Exception:
            return
        try:
            path = gfile.get_path()
            if self._show_timestamps:
                lines = [
                    f"{self._format_ts(ts)}  {line}"
                    for line, _, ts in self._log_entries
                ]
            else:
                lines = [line for line, _, _ts in self._log_entries]
            Path(path).write_text("\n".join(lines) + "\n")
        except Exception as exc:
            self.append_log(f"⚠ Save log failed: {exc}")

    def _build_log_context_popover(self) -> Gtk.Popover:
        """Build the right-click context menu popover for the log view."""
        pop = Gtk.Popover()
        pop.set_has_arrow(False)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        vbox.set_margin_start(4); vbox.set_margin_end(4)
        vbox.set_margin_top(4);   vbox.set_margin_bottom(4)

        btn_line = Gtk.Button(label="Copy line")
        btn_line.add_css_class("flat")
        btn_line.connect("clicked", self._on_copy_line_at_cursor)
        vbox.append(btn_line)

        btn_sel = Gtk.Button(label="Copy selection")
        btn_sel.add_css_class("flat")
        btn_sel.connect("clicked", self._on_copy_selection)
        vbox.append(btn_sel)

        btn_all = Gtk.Button(label="Copy all visible")
        btn_all.add_css_class("flat")
        btn_all.connect("clicked", self._on_copy_all_log)
        vbox.append(btn_all)

        pop.set_child(vbox)
        return pop

    def _on_log_right_click(self, gesture, _n, x, y) -> None:
        r = Gdk.Rectangle()
        r.x, r.y, r.width, r.height = int(x), int(y), 1, 1
        self._log_ctx_popover.set_pointing_to(r)
        # Store the click iter offset so "Copy line" knows which line to copy.
        buf_x, buf_y = self._log_view.window_to_buffer_coords(
            Gtk.TextWindowType.WIDGET, int(x), int(y)
        )
        it, _ = self._log_view.get_iter_at_position(buf_x, buf_y)
        self._log_ctx_iter_offset = it.get_offset()
        self._log_ctx_popover.popup()

    def _on_log_key_pressed(self, ctrl, keyval, keycode, state) -> bool:
        ctrl_held = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl_held and keyval == Gdk.KEY_c:
            self._copy_log_to_clipboard(line_only=False)
            return True
        return False

    def _on_copy_log(self, _btn) -> None:
        """'⎘ Copy' button: copy selection or all visible log text."""
        self._copy_log_to_clipboard(line_only=False)

    def _on_copy_line_at_cursor(self, _btn) -> None:
        self._log_ctx_popover.popdown()
        buf = self._log_buf
        it = buf.get_iter_at_offset(self._log_ctx_iter_offset)
        start = it.copy(); start.set_line_offset(0)
        end   = it.copy()
        if not end.ends_line():
            end.forward_to_line_end()
        text = buf.get_text(start, end, False)
        self._put_in_clipboard(text)

    def _on_copy_selection(self, _btn) -> None:
        self._log_ctx_popover.popdown()
        self._copy_log_to_clipboard(line_only=False)

    def _on_copy_all_log(self, _btn) -> None:
        self._log_ctx_popover.popdown()
        self._copy_all_visible_log()

    def _copy_log_to_clipboard(self, *, line_only: bool) -> None:
        """Copy selected text if there's a selection, otherwise copy all visible text."""
        buf = self._log_buf
        if buf.get_has_selection():
            start, end = buf.get_selection_bounds()
            self._put_in_clipboard(buf.get_text(start, end, False))
        else:
            self._copy_all_visible_log()

    def _copy_all_visible_log(self) -> None:
        """Copy all text currently in the log buffer."""
        buf = self._log_buf
        self._put_in_clipboard(buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False))

    def _put_in_clipboard(self, text: str) -> None:
        display = self._log_view.get_display()
        if display is None:
            return
        clipboard = display.get_clipboard()
        clipboard.set(text)

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
        self._copy_curl_btn.set_visible(ready)
        self._open_api_btn.set_visible(ready)
        # Restart button: show when READY or ERROR (so user can retry after failure)
        self._restart_btn.set_visible(state in (ServerState.READY, ServerState.ERROR))
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
            import time as _time
            self._uptime_start = _time.monotonic()
            self._uptime_label.set_visible(True)
            self._uptime_label.set_text("↑ 0s")
            if self._uptime_timer is None:
                self._uptime_timer = GLib.timeout_add(5000, self._tick_uptime)
        else:
            self._uptime_label.set_visible(False)
            if self._uptime_timer is not None:
                GLib.source_remove(self._uptime_timer)
                self._uptime_timer = None
            self._uptime_start = None

    def _tick_uptime(self) -> bool:
        """Update the uptime label every 5 s while READY."""
        import time as _time
        if self._uptime_start is None:
            self._uptime_timer = None
            return False
        elapsed = int(_time.monotonic() - self._uptime_start)
        if elapsed < 60:
            self._uptime_label.set_text(f"↑ {elapsed}s")
        elif elapsed < 3600:
            self._uptime_label.set_text(f"↑ {elapsed // 60}m")
        else:
            h, m = divmod(elapsed // 60, 60)
            self._uptime_label.set_text(f"↑ {h}h {m}m")
        return True  # keep firing

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
        self._jump_error_btn.set_visible(False)
        self._update_log_count()

    def _line_visible(self, line: str, level: str) -> bool:
        if level in self._hidden_levels:
            return False
        if self._log_search_filter and self._log_search_filter not in line.lower():
            return False
        return True

    def _rebuild_log_buffer(self):
        self._log_buf.set_text("")
        self._auto_scroll = True
        for line, level, ts in self._log_entries:
            if self._line_visible(line, level):
                self._insert_line_to_buffer(line, level, ts)
        self._update_log_count()

    def _update_log_count(self):
        visible = sum(
            1 for line, lvl, _ts in self._log_entries if self._line_visible(line, lvl)
        )
        total = len(self._log_entries)
        if visible == total:
            self._log_count_lbl.set_text(f"{total} lines")
        else:
            self._log_count_lbl.set_text(f"{visible}/{total} lines")

    def _on_ts_toggled(self, btn) -> None:
        self._show_timestamps = btn.get_active()
        self._rebuild_log_buffer()

    def _on_filter_toggled(self, btn, level: str):
        if btn.get_active():
            self._hidden_levels.discard(level)
        else:
            self._hidden_levels.add(level)
        self._rebuild_log_buffer()

    def _on_log_search_changed(self, entry) -> None:
        self._log_search_filter = entry.get_text().strip().lower()
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

    def load_bench_history(self, history: list) -> None:
        """Populate the HISTORY buffer from persisted benchmark entries (newest first)."""
        buf = self._bench_history_buf
        buf.set_text("")
        if not history:
            buf.set_text("No benchmark history yet.")
            return
        lines = []
        for r in history:
            icon = {"PASS": "✓", "BELOW_TARGET": "⚠", "FAIL": "✗"}.get(r.get("tier_pass", ""), "?")
            lines.append(
                f"{icon} {r.get('tier_pass','?'):12s}"
                f"  {r.get('model_name','?')[:20]:20s}"
                f"  {r.get('device','?'):8s}"
                f"  ISL={r.get('isl','?')} OSL={r.get('osl','?')}"
                f"  TPS={r.get('mean_tps',0):.1f}"
                f"  TTFT={r.get('mean_ttft_ms',0):.0f}ms"
                f"  [{r.get('timestamp','')[:16]}]"
            )
        buf.set_text("\n".join(lines))

    def _on_bench_export_csv(self, _btn) -> None:
        import datetime
        dialog = Gtk.FileDialog()
        dialog.set_title("Export benchmark history as CSV")
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dialog.set_initial_name(f"tt-benchmarks-{ts}.csv")
        dialog.save(self.get_root(), None, self._on_bench_export_csv_done, None)

    def _on_bench_export_csv_done(self, dialog, result, _data) -> None:
        import csv, io
        try:
            gfile = dialog.save_finish(result)
        except Exception:
            return
        from app_settings import settings as _settings
        history = list(reversed(_settings.benchmark_history or []))
        if not history:
            return
        fields = [
            "timestamp", "model_name", "device", "isl", "osl", "concurrency",
            "mean_ttft_ms", "p95_ttft_ms", "mean_tps", "tps_decode",
            "mean_e2el_ms", "request_throughput", "tier_pass",
        ]
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(history)
        try:
            Path(gfile.get_path()).write_text(out.getvalue())
        except Exception as exc:
            pass  # silent fail — user can retry

    def _on_bench_clear_history(self, _btn) -> None:
        from app_settings import settings as _settings
        _settings.benchmark_history = []
        _settings.save()
        self._bench_history_buf.set_text("History cleared.")

    def append_bench_result(self, result) -> None:
        """Append a formatted BenchResult row to the session results buffer, then refresh history."""
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
        # Refresh the persistent history section
        from app_settings import settings as _settings
        self.load_bench_history(list(reversed(_settings.benchmark_history or [])))

    def set_curl_context(self, port: str, model_repo: str) -> None:
        """Store port and model for use in the copy-curl command."""
        self._curl_port = port
        self._curl_model = model_repo

    def _on_copy_curl(self, _btn) -> None:
        port = getattr(self, "_curl_port", "8000")
        model = getattr(self, "_curl_model", "default")
        cmd = (
            f'curl http://localhost:{port}/v1/chat/completions \\\n'
            f'  -H "Content-Type: application/json" \\\n'
            f'  -d \'{{"model": "{model}", "messages": [{{"role": "user", "content": "Hello!"}}]}}\''
        )
        clipboard = self._log_view.get_clipboard()
        clipboard.set(cmd)
        self._copy_curl_btn.set_label("✓ copied")
        GLib.timeout_add(2000, lambda: self._copy_curl_btn.set_label("⧉ curl") or False)

    def _on_open_api(self, _btn) -> None:
        port = getattr(self, "_curl_port", "8000")
        import subprocess
        url = f"http://localhost:{port}/docs"
        try:
            subprocess.Popen(["xdg-open", url])
        except FileNotFoundError:
            try:
                subprocess.Popen(["open", url])
            except Exception:
                pass

    def set_ad_cards(self, cards: list) -> None:
        """Update the rotating ad unit card pool."""
        self._ad_unit.set_cards(cards)

    def update_star_btn(self, visible: bool, starred: bool,
                        on_toggle: Optional[callable] = None) -> None:
        """Show/hide the ⭐ star button and update its label to reflect current state."""
        self._star_btn.set_visible(visible)
        if visible:
            self._star_btn.set_label("★" if starred else "☆")
            self._star_btn.set_tooltip_text(
                "Unpin from Starred" if starred else "Pin to Starred"
            )
            if on_toggle:
                try:
                    self._star_btn.disconnect_by_func(on_toggle)
                except Exception:
                    pass
                self._star_btn.connect("clicked", lambda _: on_toggle())

    def set_ad_select_model_callback(self, callback) -> None:
        self._ad_unit.set_on_select_model(callback)

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
        self._running_server_bar: Optional[Gtk.InfoBar] = None

        # Outer vertical box: optional running-server banner + paned content
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(_settings.sidebar_width)
        paned.set_vexpand(True)

        self._sidebar = Sidebar(
            on_launch=self._on_launch_clicked,
            on_stop=lambda: self._ctrl.stop(),
            on_model_select=self._on_model_select,
            on_device_select=self._on_device_select,
            on_repo_change=self._on_repo_change,
            on_reset=self._on_hardware_reset,
            on_pull=self._on_pull_repo,
        )
        paned.set_start_child(self._sidebar)
        paned.set_resize_start_child(False)

        self._panel = MainPanel()
        paned.set_end_child(self._panel)
        outer.append(paned)
        self.set_child(outer)
        self._outer = outer
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
        controller.on_running_servers = self._on_running_servers_detected
        controller.on_hardware_status = self._on_hardware_status
        controller.on_compat_catalog_loaded = self._on_compat_catalog_loaded

        # Connect the ↻ chip-telemetry refresh button to the controller.
        self._sidebar._hw_refresh_btn.connect(
            "clicked", lambda _: self._ctrl.refresh_hardware_status()
        )

        # Connect the ↺ restart button to the controller.
        self._panel._restart_btn.connect(
            "clicked", lambda _: self._ctrl.restart()
        )

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
        else:
            # No repo found — show first-run setup guide in the welcome panel.
            GLib.idle_add(lambda: self._panel.show_welcome(setup_guide=True) or False)

        # Populate benchmark history from persisted data on startup.
        GLib.idle_add(lambda: self._panel.load_bench_history(self._ctrl.get_bench_history()) or False)

        # Register window-level keyboard shortcuts.
        _kc = Gtk.EventControllerKey()
        _kc.connect("key-pressed", self._on_window_key_pressed)
        self.add_controller(_kc)

    # ── Callbacks pushed by AppController ────────────────────────────────────

    def _on_state_changed(self, state: ServerState, info: str) -> None:
        """React to server state transitions: update banner, lock sidebar,
        and navigate the main panel stack to the appropriate page."""
        self._panel.set_state(state, info)
        self._sidebar.set_locked(
            state not in (ServerState.IDLE, ServerState.ERROR, ServerState.DONE)
        )

        # Update window title to reflect active model and state.
        entry = self._ctrl.current_entry
        label, _ = _STATE_LABELS.get(state, (state.name, ""))
        if entry and state not in (ServerState.IDLE, ServerState.STOPPING):
            self.set_title(f"TT Model Runner — {entry.display_name} [{label}]")
        else:
            self.set_title("TT Model Runner")

        # Navigate the main-panel stack based on the new state.
        if state == ServerState.IDLE:
            # Back to config so the user can adjust settings and re-launch.
            entry = self._ctrl.current_entry
            if entry:
                self._panel.show_config(entry, self._on_options_changed)
            else:
                self._panel.show_welcome()
        elif state == ServerState.ERROR:
            # Keep the logs visible so the user can read the failure output.
            # The ERROR pill in the banner already signals the bad state.
            self._panel.show_logs()
        elif state == ServerState.LAUNCHING:
            # Clear stale logs from the previous run before streaming new ones.
            self._panel.clear_log()
            self._panel.show_logs()
        elif state in (ServerState.RUNNING,):
            self._panel.show_logs()
        elif state == ServerState.DONE:
            # Script finished cleanly — stay on logs briefly so user can read the output.
            # Sidebar is already unlocked (set_locked above), ready for the next run.
            self._panel.show_logs()
        elif state == ServerState.READY:
            # Wire the Send button and bench run button on first READY transition (idempotent).
            self._wire_tool_send()
            self._wire_bench_run()
            # Give the copy-curl button the right port and model repo.
            port = self._sidebar.get_port()
            entry = self._ctrl.current_entry
            model_repo = entry.hf_model_repo if entry else "default"
            self._panel.set_curl_context(port, model_repo)
            # Send a desktop notification so users know the model is ready even if the
            # window is hidden behind other apps.
            self._notify_ready(entry)

    def _notify_ready(self, entry) -> None:
        """Send a desktop notification announcing the model is ready."""
        try:
            note = Gio.Notification.new("Model ready")
            name = entry.display_name if entry else "Model"
            note.set_body(f"{name} is loaded and serving requests.")
            note.set_priority(Gio.NotificationPriority.NORMAL)
            self.get_application().send_notification("model-ready", note)
        except Exception:
            pass  # notifications are best-effort

    def _on_device_select(self, device: str) -> None:
        """Rebuild the ad unit card pool when the user picks a different device."""
        self._refresh_ad_unit()

    def _on_pull_repo(self) -> None:
        """Run git pull on the inference-server repo and refresh the git info label."""
        self._panel.show_logs()

        def _after(success: bool, summary: str) -> None:
            branch, sha = self._ctrl.get_repo_git_info()
            self._sidebar.refresh_git_info(branch, sha)

        self._ctrl.pull_repo(on_complete=_after)

    def _on_hardware_status(self, chips: list) -> None:
        """Update the sidebar chip-telemetry grid when tt-smi data arrives."""
        self._sidebar.update_hardware_status(chips)

    def _on_running_servers_detected(self, servers: list) -> None:
        """Show a banner when already-running TT inference containers are found."""
        if not servers:
            return
        # Only show if we're IDLE — don't interrupt an active launch
        from server_manager import ServerState
        if self._ctrl.state not in (ServerState.IDLE, ServerState.ERROR):
            return
        # Remove any previous bar
        if self._running_server_bar:
            self._outer.remove(self._running_server_bar)
            self._running_server_bar = None

        server = servers[0]   # show banner for the first/most-recent one
        extra = f"  (+ {len(servers) - 1} more)" if len(servers) > 1 else ""
        port = server.port or "8000"

        bar = Gtk.InfoBar()
        bar.set_message_type(Gtk.MessageType.INFO)
        bar.set_show_close_button(True)
        content = bar.get_content_area()
        lbl = Gtk.Label()
        lbl.set_markup(
            f"<b>Running server detected</b>  ·  {server.container_name}  ·  "
            f"port {port}  ·  {server.running_for}{extra}"
        )
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        content.append(lbl)
        reconnect_btn = bar.add_button("Reconnect", Gtk.ResponseType.ACCEPT)
        reconnect_btn.add_css_class("suggested-action")

        def _on_response(b, resp):
            if resp == Gtk.ResponseType.ACCEPT:
                self._do_reconnect(port, server.container_name)
            self._outer.remove(b)
            self._running_server_bar = None

        bar.connect("response", _on_response)
        # Insert banner at top (before the paned widget)
        self._outer.prepend(bar)
        self._running_server_bar = bar

    def _do_reconnect(self, port: str, container_name: str) -> None:
        """Reconnect to an existing inference server container."""
        self._panel.clear_log()
        self._panel.show_logs()
        self._sidebar.set_locked(True)
        self._ctrl.adopt_running_server(port, container_name)

    def _refresh_ad_unit(self) -> None:
        """Rebuild the AdUnit card pool from the current catalog + compat catalog."""
        from ad_facts import get_all_cards
        cards = get_all_cards(
            self._ctrl.catalog,
            self._sidebar.get_selected_device(),
            self._ctrl.compat_catalog,
        )
        self._panel.set_ad_cards(cards)

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
        self._sidebar.on_compat_select = self._on_compat_select
        # Pass compat catalog if already available (fetched from disk cache).
        if self._ctrl.compat_catalog:
            self._sidebar.set_compat_catalog(self._ctrl.compat_catalog)
        branch, sha = self._ctrl.get_repo_git_info()
        self._sidebar.refresh_git_info(branch, sha)
        self._panel.set_ad_select_model_callback(self._sidebar.select_model_by_id)
        self._refresh_ad_unit()

    def _on_compat_select(self, compat_entry) -> None:
        """Show the DISCOVER info panel for a compat-catalog entry."""
        self._panel.show_discover(
            compat_entry,
            on_run=self._ctrl.launch_dev_image,
        )

    def _on_compat_catalog_loaded(self, catalog) -> None:
        """Pass the freshly-fetched compat catalog to the sidebar."""
        if catalog:
            self._sidebar.set_compat_catalog(catalog)
            self._refresh_ad_unit()

    # ── User action handlers (called from Sidebar widgets) ────────────────────

    def _on_launch_clicked(self, entry: ModelEntry, port: str) -> None:
        """Collect current options from the config panel and ask the controller
        to start the server.  If the engine family changed since the last launch,
        show a dialog recommending tt-smi -r first."""
        warning = self._ctrl.needs_reset_warning(entry)
        if warning:
            old_engine, new_engine, old_model = warning
            self._show_reset_warning_dialog(entry, port, old_engine, new_engine, old_model)
        else:
            self._ctrl.launch(entry, port, self._panel.get_options())

    def _show_reset_warning_dialog(self, entry: ModelEntry, port: str,
                                   old_engine: str, new_engine: str,
                                   old_model: str) -> None:
        """Warn that the inference engine changed and a hardware reset is recommended."""
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            text="Hardware reset recommended",
        )
        dialog.format_secondary_text(
            f"Last launch used the '{old_engine}' engine  ({old_model}).\n"
            f"Switching to '{new_engine}' — running tt-smi -r first is\n"
            f"strongly recommended to avoid hangs or device errors."
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Launch Anyway", Gtk.ResponseType.REJECT)
        reset_btn = dialog.add_button("↺  Reset & Launch", Gtk.ResponseType.ACCEPT)
        reset_btn.add_css_class("suggested-action")
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)

        def _on_response(dlg, resp: int) -> None:
            dlg.destroy()
            if resp == Gtk.ResponseType.ACCEPT:
                self._do_reset_and_launch(entry, port)
            elif resp == Gtk.ResponseType.REJECT:
                self._ctrl.launch(entry, port, self._panel.get_options())
            # CANCEL → do nothing

        dialog.connect("response", _on_response)
        dialog.present()

    def _do_reset_and_launch(self, entry: ModelEntry, port: str) -> None:
        """Run tt-smi -r then immediately launch the server on completion."""
        self._panel.show_logs()
        opts = self._panel.get_options()

        def _after_reset(success: bool) -> None:
            # Launch regardless of reset success — logs already show any error.
            self._ctrl.launch(entry, port, opts)

        self._ctrl.reset_hardware(on_complete=_after_reset)

    def _on_hardware_reset(self) -> None:
        """Called from the sidebar Reset button — run tt-smi -r and stream output."""
        self._panel.show_logs()
        self._ctrl.reset_hardware()

    def _on_model_select(self, entry: ModelEntry) -> None:
        """Tell the controller a new model was selected and update the banner
        and config panel immediately so the user sees feedback."""
        self._ctrl.select_model(entry)
        self._panel._banner_info.set_text(
            f"{entry.display_name}  ·  {entry.device_type}"
            f"  ·  {entry.inference_engine}"
        )
        self._panel.show_config(entry, self._on_options_changed)
        self._panel.set_dev_launch_callback(self._on_dev_launch)
        # Show hardware compatibility from compat catalog if available
        cat = self._ctrl.compat_catalog
        if cat is not None:
            compat = cat.lookup_by_display_name(entry.display_name)
            self._panel.set_compat_info(compat)
        # Update star button
        self._update_star_btn(entry)

    def _update_star_btn(self, entry: Optional[ModelEntry] = None) -> None:
        """Refresh the banner star button for the given (or current) entry."""
        e = entry or self._ctrl.current_entry
        if e is None:
            self._panel.update_star_btn(False, False)
            return
        starred = self._ctrl.is_starred(e)
        def _toggle():
            self._ctrl.toggle_star(e)
            self._update_star_btn(e)
            # Rebuild the tree so the STARRED section updates immediately.
            if self._sidebar._selected_device:
                self._sidebar._rebuild_tree([self._sidebar._selected_device])
            else:
                self._sidebar._rebuild_tree(None)
        self._panel.update_star_btn(True, starred, _toggle)

    def _on_options_changed(self, options) -> None:
        """Relay ConfigPanel option changes to the controller (e.g. for live
        command-preview updates or validation)."""
        self._ctrl.set_options(options)

    def _on_dev_launch(self, compat_id: str, sw_stack: str) -> None:
        """Launch a model via the tt-developer-image container."""
        self._panel.show_logs()
        self._ctrl.launch_dev_image(compat_id, sw_stack)

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

    # ── Global keyboard shortcuts ─────────────────────────────────────────────

    def _on_window_key_pressed(self, ctrl, keyval, keycode, state) -> bool:
        """Handle window-level keyboard shortcuts.

        F5           — launch (if idle/error) or stop (if active)
        Ctrl+K       — jump log to last error
        Ctrl+Shift+S — save log to file
        Ctrl+1..5    — switch main panel tab (config/logs/tools/bench/discover)
        """
        ctrl_held  = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift_held = bool(state & Gdk.ModifierType.SHIFT_MASK)

        if keyval == Gdk.KEY_F5:
            active_states = (
                ServerState.LAUNCHING, ServerState.PULLING_IMAGE,
                ServerState.LOADING, ServerState.READY, ServerState.RUNNING,
            )
            if self._ctrl.state in active_states:
                self._ctrl.stop()
            elif self._ctrl.state in (ServerState.IDLE, ServerState.ERROR, ServerState.DONE):
                self._on_launch_clicked()
            return True

        if ctrl_held and not shift_held and keyval == Gdk.KEY_k:
            self._panel._jump_error_btn.emit("clicked")
            return True

        if ctrl_held and shift_held and keyval == Gdk.KEY_S:
            self._panel._save_log_btn.emit("clicked")
            return True

        if ctrl_held and not shift_held:
            tab_map = {
                Gdk.KEY_1: "config",
                Gdk.KEY_2: "logs",
                Gdk.KEY_3: "tools",
                Gdk.KEY_4: "bench",
            }
            if keyval in tab_map:
                self._panel._stack.set_visible_child_name(tab_map[keyval])
                return True

        return False
