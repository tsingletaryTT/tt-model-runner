#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Main application window: sidebar + main panel with full state machine.

Threading discipline (CRITICAL):
    GTK is single-threaded. Worker threads must NEVER touch widgets directly.
    Every UI update from a thread must be posted via GLib.idle_add or idle_add_once.

State machine:
    IDLE → LAUNCHING → PULLING_IMAGE → LOADING → READY
                                              ↘ ERROR
           ↑                                        ↓
           └──────────── STOPPING ─────────────────┘
"""
import os
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import GLib, Gtk, Pango

from app_settings import settings as _settings
from device_detector import detect_devices
from model_catalog import ModelCatalog, ModelEntry
from server_manager import LaunchConfig, ServerManager, ServerState
from health_worker import HealthWorker
from timing_store import TimingStore
from worker import idle_add_once

_CONFIG_DIR = Path.home() / ".config" / "tt-runner-gui"
_TIMING_PATH = _CONFIG_DIR / "timing.json"

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

_STAGE_LABELS = {
    "engine_init":    "Engine Init",
    "device_setup":   "Device Mesh",
    "loading_weights":"Weights",
    "kv_cache":       "KV Cache",
    "api_startup":    "API Server",
    "trace_capture":  "Trace Capture",
    "device_init":    "Device Init",
    "mesh_created":   "Mesh",
    "cache_loading":  "TT Cache",
    "model_loaded":   "Model",
    "warmup":         "Warmup",
    "warmup_complete":"Warmup",
}

_VLLM_STAGES  = ["engine_init", "device_setup", "loading_weights", "kv_cache", "api_startup", "trace_capture"]
_MEDIA_STAGES = ["device_init", "mesh_created", "loading_weights", "cache_loading", "model_loaded", "warmup"]

_LOG_COLORS = {
    "DEBUG":    "#607D8B",
    "INFO":     "#E8F0F2",
    "WARN":     "#F4C471",
    "WARNING":  "#F4C471",
    "ERROR":    "#FF6B6B",
    "CRITICAL": "#FF6B6B",
}

_TOUR_EDU = {
    "engine_init":    "TT Metal opens PCIe connections to each Tenstorrent chip and verifies firmware. No model weights loaded yet — this is pure hardware bring-up.",
    "device_setup":   "Tensor parallelism: weight matrices are sharded column-wise across chips via Ethernet fabric. Each chip holds 1/N of every weight tensor.",
    "loading_weights":"Weight shards stream from disk into each chip's DRAM. Reading is sequential per shard — the bottleneck is PCIe bandwidth (~7 GB/s per chip).",
    "kv_cache":       "KV cache blocks are pre-allocated in SRAM. For a 32k context window, K and V each need [32768 × head_dim] × num_layers tokens of space.",
    "api_startup":    "The HTTP server starts accepting requests. The model is on-device but not yet JIT-compiled for every context length.",
    "trace_capture":  "vLLM JIT-compiles a separate execution graph for each of 10 context lengths (128 → 65408 tokens). After this, every inference reuses a pre-compiled trace — zero recompile overhead.",
    "device_init":    "The media server initializes the device mesh and allocates shared memory pools across all dies.",
    "mesh_created":   "A 2D mesh of Tenstorrent chips is established. Activations flow over the on-package fabric rather than through host DRAM.",
    "cache_loading":  "Pre-compiled TT Metal kernel binaries are loaded from tensor cache on disk. Much faster than re-compiling kernels from scratch.",
    "model_loaded":   "All model components (transformer, text encoder, VAE) are resident on-chip and ready for warmup.",
    "warmup":         "Warmup runs 2 full denoising passes to JIT-compile TT Metal kernels and capture execution traces. One-time cost per boot — subsequent inferences are fast.",
    "warmup_complete":"Warmup complete! Waiting for health check to confirm the server is accepting requests.",
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


class MainPanel(Gtk.Box):
    """Right panel: status banner, sub-stage stepper, progress bar, tour panel, log view."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._auto_scroll = True
        self._pulse_source: Optional[int] = None
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
        tour_outer.append(tour_inner)
        self._tour_rev.set_child(tour_outer)
        self.append(self._tour_rev)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Log view
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
        self.append(log_scroll)

    def _on_scroll(self, adj):
        self._auto_scroll = adj.get_value() >= adj.get_upper() - adj.get_page_size() - 10

    def _on_adj_changed(self, adj):
        if self._auto_scroll:
            adj.set_value(adj.get_upper() - adj.get_page_size())

    def append_log(self, line: str):
        import re
        buf = self._log_buf
        end = buf.get_end_iter()

        level_tag = None
        for lvl in ("ERROR", "CRITICAL", "WARN", "WARNING", "INFO", "DEBUG"):
            if re.search(rf'\b{lvl}\b', line):
                level_tag = f"lvl_{lvl}"
                break

        if buf.get_char_count() > 0:
            buf.insert(end, "\n")
            end = buf.get_end_iter()

        start_off = end.get_offset()
        buf.insert(end, line)
        if level_tag:
            s = buf.get_iter_at_offset(start_off)
            buf.apply_tag_by_name(level_tag, s, buf.get_end_iter())

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
        self._auto_scroll = True


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(
            title="TT Model Runner",
            default_width=_settings.window_width,
            default_height=_settings.window_height,
            **kwargs,
        )

        self._server_mgr = ServerManager()
        self._health_worker: Optional[HealthWorker] = None
        self._timing = TimingStore(_TIMING_PATH)
        self._catalog: Optional[ModelCatalog] = None
        self._current_entry: Optional[ModelEntry] = None
        self._state = ServerState.IDLE
        self._load_start: Optional[float] = None
        self._progress_source: Optional[int] = None

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(_settings.sidebar_width)

        self._sidebar = Sidebar(
            on_launch=self._on_launch,
            on_stop=self._on_stop,
            on_model_select=self._on_model_select,
            on_device_select=lambda d: None,
            on_repo_change=self._load_repo,
        )
        paned.set_start_child(self._sidebar)
        paned.set_resize_start_child(False)

        self._panel = MainPanel()
        paned.set_end_child(self._panel)
        self.set_child(paned)
        self.connect("close-request", self._on_close)

        # Auto-discover repo on startup
        repo_path = None
        saved = _settings.server_repo_path
        if saved:
            p = Path(saved)
            if (p / "run.py").exists() and (p / "model_spec.json").exists():
                repo_path = p
        if not repo_path:
            repo_path = self._discover_repo()
        if repo_path:
            self._sidebar._repo_entry.set_text(str(repo_path))
            GLib.idle_add(self._load_repo, repo_path)

    def _discover_repo(self) -> Optional[Path]:
        for c in [Path.home() / "code" / "tt-inference-server", Path.home() / "tt-inference-server"]:
            if (c / "run.py").exists() and (c / "model_spec.json").exists():
                return c
        return None

    def _load_repo(self, path: Path) -> bool:
        spec = path / "model_spec.json"
        if not spec.exists():
            self._panel.append_log(f"⚠ model_spec.json not found at {path}")
            return False
        try:
            self._catalog = ModelCatalog.load(spec)
        except Exception as e:
            self._panel.append_log(f"⚠ Failed to parse model_spec.json: {e}")
            return False

        _settings.server_repo_path = str(path)
        _settings.save()
        self._panel.append_log(f"Loaded {len(self._catalog.all_entries())} model configurations from {spec}")

        def _detect():
            devices = detect_devices()
            compatible = devices if devices else self._catalog.all_device_types()
            if not devices:
                GLib.idle_add(lambda: self._panel.append_log("⚠ tt-smi not found — showing all devices") or False)
            idle_add_once(self._sidebar.load_catalog, self._catalog, compatible)
        threading.Thread(target=_detect, daemon=True).start()
        return False

    def _on_model_select(self, entry: ModelEntry):
        self._current_entry = entry
        self._panel._banner_info.set_text(
            f"{entry.display_name}  ·  {entry.device_type}  ·  {entry.inference_engine}"
        )

    def _on_launch(self, entry: ModelEntry, port: str):
        if self._state not in (ServerState.IDLE, ServerState.ERROR):
            return
        self._current_entry = entry
        repo_path = Path(_settings.server_repo_path)
        if not (repo_path / "run.py").exists():
            self._panel.append_log(f"⚠ run.py not found at {repo_path}")
            return

        config = LaunchConfig(
            repo_path=repo_path,
            model_name=entry.hf_model_repo,
            device=entry.device_type,
            port=port,
            hf_token=os.environ.get("HF_TOKEN"),
            no_auth=True,
        )

        self._panel.clear_log()
        self._panel.append_log(f"▶ Launching {entry.display_name} on {entry.device_type} · port {port}")
        self._transition(ServerState.LAUNCHING)

        self._health_worker = HealthWorker(
            port=port,
            on_ready=self._on_health_ready,
            on_lost=self._on_health_lost,
            engine="media" if entry.inference_engine == "media" else "vllm",
        )
        self._health_worker.start()
        self._server_mgr.launch(config, self._on_log_line, self._on_server_state)

    def _on_stop(self):
        self._transition(ServerState.STOPPING)
        if self._health_worker:
            self._health_worker.stop()
            self._health_worker = None
        self._server_mgr.stop()
        # Give docker stop up to 10s, then force-idle
        GLib.timeout_add(10000, self._force_idle)

    def _force_idle(self) -> bool:
        if self._state == ServerState.STOPPING:
            self._transition(ServerState.IDLE)
        return False

    def _on_log_line(self, line: str):
        self._panel.append_log(line)
        if self._state == ServerState.LOADING:
            substage = self._server_mgr.parser.last_substage
            if substage:
                self._panel.set_stepper(self._build_stepper_text(substage))
                self._panel.set_tour(*self._build_tour_content(substage))

    def _on_server_state(self, state: ServerState):
        self._transition(state)

    def _on_health_ready(self, models: List[str]):
        if self._state in (ServerState.LOADING, ServerState.LAUNCHING, ServerState.PULLING_IMAGE):
            self._transition(ServerState.READY)
            mstr = ", ".join(models) if models else "ready"
            self._panel.set_state(ServerState.READY, f"localhost:{_settings.last_port}  ·  {mstr}")
            if self._load_start and self._current_entry:
                dur = time.monotonic() - self._load_start
                self._timing.record_load(
                    self._current_entry.hf_model_repo, self._current_entry.device_type, dur, cold=False
                )

    def _on_health_lost(self):
        if self._state == ServerState.READY:
            self._transition(ServerState.ERROR)
            self._panel.append_log("⚠ Health check lost — server may have crashed")

    def _transition(self, state: ServerState):
        if state == self._state:
            return
        prev = self._state
        self._state = state

        info = ""
        if self._current_entry:
            info = f"localhost:{_settings.last_port}  ·  {self._current_entry.display_name}  ·  {self._current_entry.device_type}"

        self._panel.set_state(state, info)
        self._sidebar.set_locked(state not in (ServerState.IDLE, ServerState.ERROR))

        if state == ServerState.LOADING:
            self._load_start = time.monotonic()
            self._start_progress_ticker()
        elif state in (ServerState.READY, ServerState.ERROR, ServerState.IDLE, ServerState.STOPPING):
            self._stop_progress_ticker()

    def _start_progress_ticker(self):
        if not self._progress_source:
            self._progress_source = GLib.timeout_add(1000, self._progress_tick)

    def _stop_progress_ticker(self):
        if self._progress_source:
            GLib.source_remove(self._progress_source)
            self._progress_source = None

    def _progress_tick(self) -> bool:
        if self._state != ServerState.LOADING or not self._current_entry:
            return False

        elapsed = time.monotonic() - (self._load_start or time.monotonic())
        parser = self._server_mgr.parser

        # vLLM trace capture — most reliable deterministic progress
        if parser.trace_capture_count > 0:
            frac = parser.trace_capture_count / 10.0
            remaining = (10 - parser.trace_capture_count) * 3
            self._panel.set_progress(frac, f"Capturing traces {parser.trace_capture_count}/10 · ~{remaining:.0f}s remaining")
            return True

        # WAN 2.2 / media warmup progress
        if parser.warmup_n is not None and parser.warmup_total:
            frac = parser.warmup_n / parser.warmup_total
            est = self._timing.estimate_substage(
                self._current_entry.hf_model_repo, self._current_entry.device_type, "warmup"
            )
            label = f"Warmup {parser.warmup_n}/{parser.warmup_total}"
            if est.seconds:
                per_step = est.seconds / parser.warmup_total
                remaining = per_step * (parser.warmup_total - parser.warmup_n)
                label += f" · ~{remaining:.0f}s remaining · {est.source}"
            self._panel.set_progress(frac, label)
            return True

        # Time-based estimate
        est = self._timing.estimate_load(
            self._current_entry.hf_model_repo,
            self._current_entry.device_type,
            cold=False,
            size_gb=self._current_entry.min_disk_gb or 10.0,
            family=self._current_entry.family,
        )
        if est.seconds and est.seconds > 0:
            frac = min(elapsed / est.seconds, 0.95)
            remaining = max(est.seconds - elapsed, 0)
            m, s = divmod(int(remaining), 60)
            ts = f"{m}m {s}s" if m else f"{s}s"
            self._panel.set_progress(frac, f"~{ts} remaining · {est.source}")
        else:
            self._panel._progress_bar.pulse()
        return True

    def _build_stepper_text(self, substage: str) -> str:
        entry = self._current_entry
        if not entry:
            return ""
        stages = _MEDIA_STAGES if entry.inference_engine == "media" else _VLLM_STAGES
        parts = []
        found_active = False
        for s in stages:
            lbl = _STAGE_LABELS.get(s, s)
            if s == substage:
                parts.append(f"● {lbl}")
                found_active = True
            elif not found_active:
                parts.append(f"✓ {lbl}")
            else:
                parts.append(f"○ {lbl}")
        return "  ──  ".join(parts)

    def _build_tour_content(self, substage: Optional[str]):
        entry = self._current_entry
        if not entry:
            return ("", "")

        left = f"📁 {entry.hf_model_repo}\n"
        if entry.min_disk_gb:
            left += f"  ~{entry.min_disk_gb:.0f} GB on disk\n"
        if entry.param_count:
            left += f"  {entry.param_count:.0f}B parameters\n"
        left += f"  Engine: {entry.inference_engine}\n"
        left += f"  Status: {entry.status}"

        right = _TOUR_EDU.get(substage or "", "Loading model onto Tenstorrent hardware…")
        return (left, right)

    def _on_close(self, win):
        _settings.window_width = self.get_width()
        _settings.window_height = self.get_height()
        _settings.save()
        if self._state not in (ServerState.IDLE, ServerState.ERROR):
            self._on_stop()
        return False
