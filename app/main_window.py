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
from hf_cache import ModelCacheInfo, scan_model_cache
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

_TOUR_CARDS: dict = {
    "engine_init": [
        "TT Metal opens PCIe connections to each Tenstorrent chip and verifies firmware. No model weights loaded yet — this is pure hardware bring-up.",
        "Each Tenstorrent chip has 108 Tensix cores in a 12×9 grid. Each core has 1.5 MB of local SRAM and a dedicated matrix/vector compute unit that runs independently.",
        "An embedded RISC-V management CPU on each chip handles DMA scheduling, Ethernet link setup, and power management while Tensix cores later run inference.",
    ],
    "device_setup": [
        "Tensor parallelism: weight matrices are sharded column-wise across chips via Ethernet fabric. Each chip holds 1/N of every weight tensor.",
        "The Ethernet links between chips run at 100 Gb/s. An allreduce across 4 chips takes ~1 µs — far less than a single transformer layer's compute time.",
        "Column-parallel sharding means each chip computes attention for a different subset of heads. With GQA the KV heads are fewer, reducing KV replication cost.",
    ],
    "loading_weights": [
        "Weight shards stream from disk into each chip's DRAM via PCIe (~7 GB/s per chip). The bottleneck is disk→DRAM transfer, not computation.",
        "Weights arrive as bfloat16 (2 bytes/element). The chips support on-the-fly quantization to int8 to halve DRAM bandwidth during actual inference.",
        "Attention weight matrices (Q, K, V, O_proj) are column-sharded across chips. Each chip's Q_proj is [hidden × (hidden÷N)] where N is the chip count.",
    ],
    "kv_cache": [
        "KV cache is pre-allocated in chip SRAM before the first token. Accessing SRAM takes <1 µs vs. ~100 ns for DRAM — critical for fast autoregressive decode.",
        "Each layer needs 2 × context_length × head_dim × num_kv_heads elements for K and V. With GQA the KV tensor is much smaller than the full attention map.",
        "Paged KV attention divides the cache into fixed-size blocks (e.g., 16 tokens each), avoiding fragmentation and enabling efficient batch scheduling.",
    ],
    "api_startup": [
        "The HTTP server starts accepting connections. The model is fully on-device but execution graphs haven't been JIT-compiled for every context length yet.",
        "vLLM uses continuous batching: new requests join the decode batch mid-generation, keeping hardware utilization high even with uneven arrival rates.",
        "The vLLM scheduler can preempt a partially-decoded sequence and swap it out when memory is needed — enabling fair multi-tenant serving without starvation.",
    ],
    "trace_capture": [
        "vLLM JIT-compiles a separate execution graph for each of 10 context lengths (128→65408 tokens). After capture, every inference replays a pre-built trace.",
        "Trace capture = graph compilation: TT Metal unrolls every op into a static kernel-dispatch sequence. At inference time there is zero Python GIL overhead.",
        "Each of the 10 traces is specialized for its sequence length — the compiler tiles and schedules operations differently for a 128-token vs. 65408-token batch.",
    ],
    "device_init": [
        "The media server initializes the device mesh and allocates shared memory pools across all dies. This is the first time the hardware is exercised.",
        "On a Galaxy (8× P150), the mesh is 8 chips in a ring topology. Each chip sees a unified virtual address space backed by its own 12 GB LPDDR5 DRAM.",
        "Device init checks firmware versions and calibrates thermal sensors. If any chip is above the thermal threshold, the server refuses to start.",
    ],
    "mesh_created": [
        "A 2D chip mesh is established. Activations flow over the on-package Ethernet fabric rather than through host DRAM — reducing round-trip latency by ~100×.",
        "The mesh topology determines model partitioning. For video: spatial encoder on one chip set, temporal decoder on another, pipelined across the fabric.",
        "Collective operations (AllReduce, AllGather) use ring algorithms that saturate the Ethernet links without touching host memory.",
    ],
    "cache_loading": [
        "Pre-compiled TT Metal kernel binaries are loaded from disk cache. A cache hit avoids LLVM compilation — saving minutes per model load on first boot.",
        "The tensor cache keys on op type + tensor shape + data type + chip generation. A Wormhole kernel binary won't be used on Blackhole — different ISA.",
        "The cache is invalidated on firmware update to prevent ABI mismatches. After an upgrade the first load is slower; subsequent loads are fast again.",
    ],
    "model_loaded": [
        "All model components — transformer, text encoder, and VAE decoder — are resident on-chip and ready for the warmup pass.",
        "For diffusion models the pipeline is: text encoder (CLIP or T5) → denoising U-Net or DiT → VAE decoder. All three sub-models are pre-loaded onto chips.",
        "The VAE decoder is the final inference step, converting a latent 64×64 tensor into a 1024×1024 pixel image. It's the compute-heaviest part per output pixel.",
    ],
    "warmup": [
        "Warmup runs 2 full denoising passes to JIT-compile TT Metal kernels and capture execution traces. One-time cost per boot; subsequent inferences are fast.",
        "WAN 2.2 uses ~50 denoising timesteps per video. Each warmup pass compiles attention and FFN kernels for that specific resolution and batch size.",
        "After warmup the compiled kernels are stored in SRAM. Subsequent inferences skip compilation and replay the recorded kernel dispatch sequence directly.",
    ],
    "warmup_complete": [
        "Warmup complete! The server is now fully primed: kernels compiled, traces captured, KV cache allocated. Waiting for health check.",
        "The health endpoint (/tt-liveness or /v1/models) returns 200 once the server thread is ready — this guard prevents routing traffic before the model is live.",
        "First inference will be nearly as fast as steady-state. Compile-time overhead was paid during warmup; the hot path is now pure kernel replay.",
    ],
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
        self._cache_info: Optional[ModelCacheInfo] = None
        self._state = ServerState.IDLE
        self._load_start: Optional[float] = None
        self._progress_source: Optional[int] = None
        self._tour_card_idx: int = 0
        self._tour_card_source: Optional[int] = None
        self._tour_substage: Optional[str] = None

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
        self._cache_info = None
        self._panel._banner_info.set_text(
            f"{entry.display_name}  ·  {entry.device_type}  ·  {entry.inference_engine}"
        )
        # Show the configuration panel so the user can review/change launch
        # options before hitting Launch.  ConfigPanel is created lazily on
        # the first call and reused for subsequent model selections.
        self._panel.show_config(entry, self._on_options_changed)

        repo = entry.hf_model_repo
        def _scan():
            info = scan_model_cache(repo)
            idle_add_once(self._on_cache_scanned, info)
        threading.Thread(target=_scan, daemon=True).start()

    def _on_options_changed(self, options) -> None:
        """Called by ConfigPanel whenever any option widget changes value.

        Currently a no-op — options are read at launch time via
        self._panel.get_options().  Kept as an extension point for live
        validation or command-preview updates in MainWindow.
        """
        pass

    def _on_cache_scanned(self, info: ModelCacheInfo):
        self._cache_info = info
        # If tour is visible right now, refresh the left panel with real data
        if self._state == ServerState.LOADING and self._tour_substage:
            self._panel.set_tour(*self._build_tour_content(self._tour_substage))

    def _read_hf_token(self, repo_path: Path) -> Optional[str]:
        """Read HF_TOKEN from environment first, then repo .env file."""
        token = os.environ.get("HF_TOKEN", "")
        if token:
            return token
        env_file = repo_path / ".env"
        if env_file.exists():
            for line in env_file.read_text(errors="replace").splitlines():
                if line.startswith("HF_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if token:
                        return token
        return None

    def _on_launch(self, entry: ModelEntry, port: str):
        if self._state not in (ServerState.IDLE, ServerState.ERROR):
            return
        self._current_entry = entry
        repo_path = Path(_settings.server_repo_path)
        if not (repo_path / "run.py").exists():
            self._panel.append_log(f"⚠ run.py not found at {repo_path}")
            return

        hf_token = self._read_hf_token(repo_path)
        if not hf_token:
            self._panel.append_log("⚠ HF_TOKEN not found in environment or .env — launch may fail")

        # Collect any options the user configured in ConfigPanel (may be None
        # if the user never selected a model and went straight to launch via
        # keyboard shortcut — in that case server_manager falls back to
        # sensible defaults).
        options = self._panel.get_options()

        config = LaunchConfig(
            repo_path=repo_path,
            model_name=entry.display_name,  # run.py --model expects short name e.g. "Wan2.2-T2V-A14B-Diffusers"
            device=entry.device_type,
            port=port,
            hf_token=hf_token,
            no_auth=True,
            options=options,
            inference_engine=entry.inference_engine,
        )

        self._panel.clear_log()
        self._panel.append_log(f"▶ Launching {entry.display_name} on {entry.device_type} · port {port}")
        # Switch to the log view before transitioning state so the user sees
        # output immediately rather than staying on the config page.
        self._panel.show_logs()
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
                if substage != self._tour_substage:
                    self._tour_substage = substage
                    self._tour_card_idx = 0
                cards = _TOUR_CARDS.get(substage, [])
                self._panel.update_tour_dots(self._tour_card_idx, len(cards))
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
            self._tour_card_idx = 0
            self._tour_substage = None
            self._start_progress_ticker()
            self._start_tour_timer()
        elif state in (ServerState.READY, ServerState.ERROR, ServerState.IDLE, ServerState.STOPPING):
            self._stop_progress_ticker()
            self._stop_tour_timer()

        # Stack navigation: return to the config page (or welcome if no model
        # is selected) when the server reaches a terminal / idle state; keep
        # the log view on all in-progress states so the user sees output.
        if state in (ServerState.IDLE, ServerState.ERROR):
            if self._current_entry:
                self._panel.show_config(self._current_entry, self._on_options_changed)
            else:
                self._panel.show_welcome()
        elif state == ServerState.LAUNCHING:
            # show_logs() is called explicitly in _on_launch before _transition
            # for the initial launch.  This branch handles the STOPPING→LAUNCHING
            # re-launch edge-case where _on_launch is called again without an
            # explicit show_logs() call preceding _transition.
            self._panel.show_logs()

    def _start_progress_ticker(self):
        if not self._progress_source:
            self._progress_source = GLib.timeout_add(1000, self._progress_tick)

    def _stop_progress_ticker(self):
        if self._progress_source:
            GLib.source_remove(self._progress_source)
            self._progress_source = None

    def _start_tour_timer(self):
        if not self._tour_card_source:
            self._tour_card_source = GLib.timeout_add(12000, self._advance_tour_card)

    def _stop_tour_timer(self):
        if self._tour_card_source:
            GLib.source_remove(self._tour_card_source)
            self._tour_card_source = None

    def _advance_tour_card(self) -> bool:
        if self._state != ServerState.LOADING:
            self._tour_card_source = None
            return False
        substage = self._tour_substage or ""
        cards = _TOUR_CARDS.get(substage, [])
        if len(cards) > 1:
            self._tour_card_idx = (self._tour_card_idx + 1) % len(cards)
            self._panel.update_tour_dots(self._tour_card_idx, len(cards))
            self._panel.set_tour(*self._build_tour_content(substage))
        return True  # keep firing

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

    def _build_tour_left(self, entry: ModelEntry) -> str:
        ci = self._cache_info
        lines = [f"📁 {entry.hf_model_repo}"]
        if ci and ci.is_cached:
            lines.append("  ✓ cached locally")
            if ci.safetensors:
                gb = ci.total_bytes / 1e9
                lines.append(f"  {len(ci.safetensors)} shards · {gb:.1f} GB")
            else:
                other_gb = ci.total_bytes / 1e9
                if other_gb > 0:
                    lines.append(f"  {other_gb:.1f} GB total")
            a = ci.arch
            if a and a.num_layers:
                lines.append(f"  {a.num_layers} layers · hidden={a.hidden_size}")
                if a.num_kv_heads and a.num_kv_heads != a.num_heads:
                    lines.append(f"  GQA: {a.num_heads}Q / {a.num_kv_heads}KV heads")
                elif a.num_heads:
                    lines.append(f"  {a.num_heads} heads · head_dim={a.head_dim}")
                if a.context_length:
                    lines.append(f"  ctx={a.context_length:,} tokens")
        elif ci and not ci.is_cached:
            lines.append("  ○ not in local HF cache")
            if entry.min_disk_gb:
                lines.append(f"  ~{entry.min_disk_gb:.0f} GB on disk")
        else:
            if entry.min_disk_gb:
                lines.append(f"  ~{entry.min_disk_gb:.0f} GB on disk")
            if entry.param_count:
                lines.append(f"  {entry.param_count:.0f}B parameters")
        lines.append(f"  Engine: {entry.inference_engine}")
        lines.append(f"  Status: {entry.status}")
        return "\n".join(lines)

    def _build_tour_content(self, substage: Optional[str]) -> tuple:
        entry = self._current_entry
        if not entry:
            return ("", "")
        left = self._build_tour_left(entry)
        cards = _TOUR_CARDS.get(substage or "", [])
        if not cards:
            right = "Loading model onto Tenstorrent hardware…"
        else:
            idx = self._tour_card_idx % len(cards)
            right = cards[idx]
        return (left, right)

    def _on_close(self, win):
        _settings.window_width = self.get_width()
        _settings.window_height = self.get_height()
        _settings.save()
        if self._state not in (ServerState.IDLE, ServerState.ERROR):
            self._on_stop()
        return False
