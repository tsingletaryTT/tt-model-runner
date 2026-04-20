#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""ConfigPanel — full-screen launch configuration widget.

Replaces the log view when idle.  Shows use-case presets, quick settings,
docker image picker, advanced fields, and a live command preview.
"""
import json
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango

# Load model descriptions from data/ at import time (once, cached).
_DESCRIPTIONS: Dict[str, str] = {}
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

def _load_descriptions() -> Dict[str, str]:
    path = _DATA_DIR / "model-descriptions.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
        return {k: v for k, v in raw.items() if k != "_meta" and isinstance(v, str)}
    except Exception:
        return {}

_DESCRIPTIONS = _load_descriptions()


def get_model_description(display_name: str) -> str:
    """Return the description for a model by its display name, or empty string."""
    return _DESCRIPTIONS.get(display_name, "")

from docker_images import DockerImage, scan_local_images
from timing_store import TimingStore

_timing_store: Optional["TimingStore"] = None

def _get_timing_store() -> "TimingStore":
    global _timing_store
    if _timing_store is None:
        _timing_store = TimingStore()
    return _timing_store
from launch_options import (
    LaunchOptions, MODEL_TYPE_USE_CASES, USE_CASE_LABELS,
    apply_preset, build_extra_args, detect_tool_parser,
)
from model_catalog import ModelEntry
from profiles import (
    delete_profile, list_profiles, profile_to_options, save_profile,
)
from worker import idle_add_once


# Known vLLM tool-call parsers (shown in parser dropdown)
_PARSERS = ["hermes", "llama3_json", "mistral", "pythonic"]

# Context length preset values for dropdown
_CTX_OPTIONS = [8192, 16384, 32768, 65536, 131072]
# Max concurrent seqs preset values
_SEQ_OPTIONS = [1, 4, 8, 16, 32, 64]


class ConfigPanel(Gtk.Box):
    """Full-screen configuration panel shown when a model is selected and idle."""

    def __init__(self, on_options_changed: Callable[[LaunchOptions], None]):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._on_options_changed = on_options_changed
        self._entry: Optional[ModelEntry] = None
        self._options = LaunchOptions()
        self._use_case_btns: dict = {}        # key → Gtk.ToggleButton
        self._docker_images: List[DockerImage] = []
        self._preview_source: Optional[int] = None
        self._inhibit_signals: bool = False   # suppress change callbacks while updating UI
        self._arch_scan_model: str = ""       # hf_model_repo of in-flight arch scan
        self._build()

    # ------------------------------------------------------------------ build

    def _build(self):
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        inner.set_margin_start(12)
        inner.set_margin_end(12)
        inner.set_margin_top(8)
        inner.set_margin_bottom(8)

        # --- Model strip ---
        self._model_strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._model_strip.set_margin_bottom(6)
        self._strip_name = Gtk.Label(label="")
        self._strip_name.set_markup("<b>Select a model</b>")
        self._strip_name.set_halign(Gtk.Align.START)
        self._strip_name.set_hexpand(True)
        self._model_strip.append(self._strip_name)
        self._strip_badge = Gtk.Label(label="")
        self._strip_badge.add_css_class("pill")
        self._strip_badge.add_css_class("pill-idle")
        self._model_strip.append(self._strip_badge)
        inner.append(self._model_strip)

        # Description label — shown when a description exists in data/model-descriptions.json
        self._strip_desc = Gtk.Label(label="")
        self._strip_desc.add_css_class("muted")
        self._strip_desc.set_halign(Gtk.Align.START)
        self._strip_desc.set_margin_bottom(4)
        self._strip_desc.set_wrap(True)
        self._strip_desc.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._strip_desc.set_visible(False)
        inner.append(self._strip_desc)

        # Metadata row — compact single-line showing HF repo path, param count, disk requirement
        # e.g. "meta-llama/Llama-3.3-70B-Instruct  ·  70B params  ·  ~141 GB disk"
        self._strip_meta = Gtk.Label(label="")
        self._strip_meta.add_css_class("muted")
        self._strip_meta.set_halign(Gtk.Align.START)
        self._strip_meta.set_margin_bottom(2)
        self._strip_meta.set_ellipsize(Pango.EllipsizeMode.END)
        self._strip_meta.set_visible(False)
        inner.append(self._strip_meta)

        # Timing estimate row — "Est. load: ~8 min  (warm, 4 samples)"
        self._strip_timing = Gtk.Label(label="")
        self._strip_timing.add_css_class("muted")
        self._strip_timing.set_halign(Gtk.Align.START)
        self._strip_timing.set_margin_bottom(2)
        self._strip_timing.set_visible(False)
        inner.append(self._strip_timing)

        # Architecture facts row — shown only when model is in HF cache
        # e.g. "80 layers · 64 heads / 8 KV · ctx 131072 · vocab 128256"
        self._strip_arch = Gtk.Label(label="")
        self._strip_arch.add_css_class("muted")
        self._strip_arch.set_halign(Gtk.Align.START)
        self._strip_arch.set_margin_bottom(2)
        self._strip_arch.set_ellipsize(Pango.EllipsizeMode.END)
        self._strip_arch.set_visible(False)
        inner.append(self._strip_arch)

        # Compat row — hardware support from the TT compatibility catalog
        # e.g. "Compat: N150 ✓  ·  N300 ✓  ·  T3K ⚠"
        self._strip_compat = Gtk.Label(label="")
        self._strip_compat.add_css_class("muted")
        self._strip_compat.set_halign(Gtk.Align.START)
        self._strip_compat.set_margin_bottom(6)
        self._strip_compat.set_ellipsize(Pango.EllipsizeMode.END)
        self._strip_compat.set_visible(False)
        inner.append(self._strip_compat)

        inner.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # --- Profile bar ---
        profile_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        profile_bar.set_margin_top(6)
        profile_bar.set_margin_bottom(6)
        lbl = Gtk.Label(label="Profile:")
        lbl.add_css_class("muted")
        profile_bar.append(lbl)
        self._profile_combo = Gtk.ComboBoxText()
        self._profile_combo.append("__none__", "No profile")
        self._profile_combo.set_active(0)
        self._profile_combo.set_hexpand(True)
        self._profile_combo.connect("changed", self._on_profile_selected)
        profile_bar.append(self._profile_combo)
        self._save_profile_btn = Gtk.Button(label="Save…")
        self._save_profile_btn.connect("clicked", self._on_save_profile)
        profile_bar.append(self._save_profile_btn)
        self._del_profile_btn = Gtk.Button(label="✕")
        self._del_profile_btn.add_css_class("destructive-action")
        self._del_profile_btn.connect("clicked", self._on_delete_profile)
        self._del_profile_btn.set_sensitive(False)
        profile_bar.append(self._del_profile_btn)
        inner.append(profile_bar)
        inner.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # --- USE CASE chips ---
        uc_lbl = Gtk.Label(label="USE CASE")
        uc_lbl.add_css_class("muted")
        uc_lbl.set_halign(Gtk.Align.START)
        uc_lbl.set_margin_top(8)
        uc_lbl.set_margin_bottom(4)
        inner.append(uc_lbl)
        self._uc_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._uc_box.set_margin_bottom(8)
        inner.append(self._uc_box)

        # --- QUICK SETTINGS ---
        qs_lbl = Gtk.Label(label="QUICK SETTINGS")
        qs_lbl.add_css_class("muted")
        qs_lbl.set_halign(Gtk.Align.START)
        qs_lbl.set_margin_bottom(4)
        inner.append(qs_lbl)

        # vLLM-only row (context + seqs + tool use)
        self._vllm_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._vllm_box.set_margin_bottom(6)

        ctx_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ctx_lbl = Gtk.Label(label="Context length")
        ctx_lbl.add_css_class("muted")
        ctx_lbl.set_hexpand(True)
        ctx_lbl.set_halign(Gtk.Align.START)
        ctx_row.append(ctx_lbl)
        self._ctx_combo = Gtk.ComboBoxText.new_with_entry()
        for v in _CTX_OPTIONS:
            self._ctx_combo.append_text(str(v))
        self._ctx_combo.append_text("default")
        self._ctx_combo.set_active(_CTX_OPTIONS.index(131072))
        self._ctx_combo.get_child().connect("changed", self._on_ctx_changed)
        ctx_row.append(self._ctx_combo)
        seq_lbl = Gtk.Label(label="Max concurrent")
        seq_lbl.add_css_class("muted")
        seq_lbl.set_hexpand(True)
        seq_lbl.set_halign(Gtk.Align.START)
        ctx_row.append(seq_lbl)
        self._seq_combo = Gtk.ComboBoxText.new_with_entry()
        for v in _SEQ_OPTIONS:
            self._seq_combo.append_text(str(v))
        self._seq_combo.append_text("default")
        self._seq_combo.set_active(len(_SEQ_OPTIONS))   # default
        self._seq_combo.get_child().connect("changed", self._on_seq_changed)
        ctx_row.append(self._seq_combo)
        self._vllm_box.append(ctx_row)

        # Tool use row
        tool_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._tool_toggle = Gtk.ToggleButton(label="🔧 Tool use")
        self._tool_toggle.connect("toggled", self._on_tool_toggled)
        tool_row.append(self._tool_toggle)
        parser_lbl = Gtk.Label(label="Parser:")
        parser_lbl.add_css_class("muted")
        tool_row.append(parser_lbl)
        self._parser_combo = Gtk.ComboBoxText()
        for p in _PARSERS:
            self._parser_combo.append_text(p)
        self._parser_combo.set_active(0)
        self._parser_combo.connect("changed", self._on_parser_changed)
        tool_row.append(self._parser_combo)
        self._auto_tool_check = Gtk.CheckButton(label="Auto tool choice")
        self._auto_tool_check.connect("toggled", self._on_any_change)
        tool_row.append(self._auto_tool_check)
        self._tool_detail_row = tool_row   # shown/hidden with tool use
        self._vllm_box.append(tool_row)

        inner.append(self._vllm_box)

        # General row (dev mode + timeout + workflow args) — shown for all engines
        gen_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        gen_row.set_margin_bottom(6)
        self._dev_mode_check = Gtk.CheckButton(label="Dev mode")
        self._dev_mode_check.connect("toggled", self._on_any_change)
        gen_row.append(self._dev_mode_check)
        self._no_timeout_check = Gtk.CheckButton(label="Disable TT timeout")
        self._no_timeout_check.connect("toggled", self._on_any_change)
        gen_row.append(self._no_timeout_check)
        wf_lbl = Gtk.Label(label="Workflow args:")
        wf_lbl.add_css_class("muted")
        gen_row.append(wf_lbl)
        self._workflow_entry = Gtk.Entry()
        self._workflow_entry.set_placeholder_text("param=value …")
        self._workflow_entry.set_hexpand(True)
        self._workflow_entry.connect("changed", self._on_any_change)
        gen_row.append(self._workflow_entry)
        inner.append(gen_row)

        # Troubleshooting row — always visible so users can act on validation failures
        trouble_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        trouble_row.set_margin_bottom(6)
        self._skip_sw_check = Gtk.CheckButton(label="Skip SW validation")
        self._skip_sw_check.set_tooltip_text(
            "--skip-system-sw-validation: bypass firmware/KMD version checks"
        )
        self._skip_sw_check.connect("toggled", self._on_any_change)
        trouble_row.append(self._skip_sw_check)
        self._no_trace_check = Gtk.CheckButton(label="Disable trace capture")
        self._no_trace_check.set_tooltip_text(
            "--disable-trace-capture: skip JIT trace compilation (faster startup, slower inference)"
        )
        self._no_trace_check.connect("toggled", self._on_any_change)
        trouble_row.append(self._no_trace_check)
        inner.append(trouble_row)
        inner.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # --- Docker image picker ---
        docker_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        docker_row.set_margin_top(6)
        docker_row.set_margin_bottom(6)
        docker_lbl = Gtk.Label(label="Docker image")
        docker_lbl.add_css_class("muted")
        docker_row.append(docker_lbl)
        self._docker_combo = Gtk.ComboBoxText()
        self._docker_combo.append("__spec__", "spec default")
        self._docker_combo.set_active(0)
        self._docker_combo.set_hexpand(True)
        self._docker_combo.connect("changed", self._on_docker_changed)
        docker_row.append(self._docker_combo)
        refresh_btn = Gtk.Button(label="⟳")
        refresh_btn.connect("clicked", self._on_docker_refresh)
        docker_row.append(refresh_btn)
        self._docker_status = Gtk.Label(label="")
        self._docker_status.add_css_class("muted")
        docker_row.append(self._docker_status)
        inner.append(docker_row)

        # --- Advanced expander ---
        exp = Gtk.Expander(label="Advanced")
        exp.set_margin_top(4)
        exp.set_margin_bottom(4)
        adv_grid = Gtk.Grid()
        adv_grid.set_column_spacing(8)
        adv_grid.set_row_spacing(4)
        adv_grid.set_margin_top(6)
        adv_grid.set_margin_start(8)

        def _adv_row(grid, row, label, widget):
            lbl = Gtk.Label(label=label)
            lbl.add_css_class("muted")
            lbl.set_halign(Gtk.Align.END)
            grid.attach(lbl, 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)

        self._vllm_extra_entry = Gtk.Entry()
        self._vllm_extra_entry.set_placeholder_text('{"block_size": 64}')
        self._vllm_extra_entry.set_hexpand(True)
        self._vllm_extra_entry.connect("changed", self._on_vllm_extra_changed)
        _adv_row(adv_grid, 0, "vLLM args (JSON)", self._vllm_extra_entry)

        self._tt_config_entry = Gtk.Entry()
        self._tt_config_entry.set_placeholder_text('{"trace_region_size": 4096}')
        self._tt_config_entry.set_hexpand(True)
        self._tt_config_entry.connect("changed", self._on_any_change)
        _adv_row(adv_grid, 1, "TT config (JSON)", self._tt_config_entry)

        self._hf_cache_entry = Gtk.Entry()
        self._hf_cache_entry.set_placeholder_text("~/.cache/huggingface")
        self._hf_cache_entry.set_hexpand(True)
        self._hf_cache_entry.connect("changed", self._on_any_change)
        _adv_row(adv_grid, 2, "Host HF cache", self._hf_cache_entry)

        self._volume_entry = Gtk.Entry()
        self._volume_entry.set_hexpand(True)
        self._volume_entry.connect("changed", self._on_any_change)
        _adv_row(adv_grid, 3, "Host volume", self._volume_entry)

        self._weights_entry = Gtk.Entry()
        self._weights_entry.set_hexpand(True)
        self._weights_entry.connect("changed", self._on_any_change)
        _adv_row(adv_grid, 4, "Weights dir", self._weights_entry)

        self._bind_entry = Gtk.Entry()
        self._bind_entry.set_placeholder_text("0.0.0.0")
        self._bind_entry.set_hexpand(True)
        self._bind_entry.connect("changed", self._on_any_change)
        _adv_row(adv_grid, 5, "Bind host", self._bind_entry)

        self._device_id_entry = Gtk.Entry()
        self._device_id_entry.set_placeholder_text("0")
        self._device_id_entry.set_hexpand(True)
        self._device_id_entry.connect("changed", self._on_any_change)
        _adv_row(adv_grid, 6, "Device ID", self._device_id_entry)

        self._image_user_entry = Gtk.Entry()
        self._image_user_entry.set_placeholder_text("1000")
        self._image_user_entry.set_hexpand(True)
        self._image_user_entry.connect("changed", self._on_any_change)
        _adv_row(adv_grid, 7, "Image user (UID)", self._image_user_entry)

        exp.set_child(adv_grid)
        inner.append(exp)
        inner.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # --- Command preview ---
        prev_lbl = Gtk.Label(label="COMMAND PREVIEW")
        prev_lbl.add_css_class("muted")
        prev_lbl.set_halign(Gtk.Align.START)
        prev_lbl.set_margin_top(6)
        inner.append(prev_lbl)
        self._preview_buf = Gtk.TextBuffer()
        self._preview_view = Gtk.TextView(buffer=self._preview_buf)
        self._preview_view.set_editable(False)
        self._preview_view.set_cursor_visible(False)
        self._preview_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._preview_view.set_monospace(True)
        self._preview_view.add_css_class("log-view")
        self._preview_view.set_size_request(-1, 60)
        inner.append(self._preview_view)

        scroll.set_child(inner)
        self.append(scroll)

    # --------------------------------------------------------------- model set

    @staticmethod
    def _make_timing_label(entry) -> str:
        """Build a human-readable load-time estimate string for entry, or ''."""
        try:
            ts = _get_timing_store()
            size_gb = entry.min_disk_gb or 0.0
            result = ts.estimate_load(
                hf_repo=entry.hf_model_repo,
                device=entry.device_type,
                cold=False,
                size_gb=size_gb,
                family=entry.family,
            )
            if result.confidence == "none" or result.seconds is None:
                return ""
            secs = result.seconds
            if secs < 60:
                time_str = f"~{secs:.0f}s"
            elif secs < 3600:
                time_str = f"~{secs / 60:.0f} min"
            else:
                time_str = f"~{secs / 3600:.1f} h"
            conf_tag = "" if result.confidence == "high" else f"  ({result.confidence} confidence)"
            return f"Est. load: {time_str}{conf_tag}"
        except Exception:
            return ""

    def set_model(self, entry: ModelEntry) -> None:
        """Update the panel for a newly selected model entry."""
        self._entry = entry
        self._inhibit_signals = True

        # Model strip
        self._strip_name.set_markup(
            f"<b>{entry.display_name}</b>"
            f"  <span foreground='#607D8B'>{entry.model_type} · {entry.inference_engine} · {entry.device_type}</span>"
        )
        badge_text = entry.status.upper()
        badge_css = {
            "PRODUCTION": "pill-ready",
            "EXPERIMENTAL": "pill-loading",
        }.get(badge_text, "pill-idle")
        self._strip_badge.set_text(badge_text)
        self._strip_badge.set_css_classes(["pill", badge_css])

        # Show description if one exists for this model
        desc = get_model_description(entry.display_name)
        self._strip_desc.set_text(desc)
        self._strip_desc.set_visible(bool(desc))

        # Populate compact metadata row: HF repo · param count · disk requirement
        parts = [entry.hf_model_repo]
        if entry.param_count:
            parts.append(f"{entry.param_count:.0f}B params")
        if entry.min_disk_gb:
            parts.append(f"~{entry.min_disk_gb:.0f} GB disk")
        meta_text = "  ·  ".join(parts)
        self._strip_meta.set_text(meta_text)
        self._strip_meta.set_visible(True)

        # Timing estimate — read from TimingStore (warm start, i.e. weights on NVMe)
        timing_text = self._make_timing_label(entry)
        self._strip_timing.set_text(timing_text)
        self._strip_timing.set_visible(bool(timing_text))

        # Architecture facts — kick off background scan of HF cache
        self._strip_arch.set_visible(False)
        self._strip_compat.set_visible(False)
        self._scan_arch_async(entry)

        # Rebuild use-case chips
        self._populate_use_cases(entry)

        # Show/hide vLLM section
        self._vllm_box.set_visible(entry.inference_engine == "vllm")

        # Apply default preset
        default_uc = MODEL_TYPE_USE_CASES.get(entry.model_type, ["dev"])[0]
        self._apply_use_case(default_uc)

        # Reload profiles for this model type
        self._refresh_profiles(entry.model_type)

        self._inhibit_signals = False

        # Refresh docker images in background
        self._refresh_docker_images(entry.docker_image)
        self._update_preview()

    # -------------------------------------------------------- arch facts scan

    def _scan_arch_async(self, entry: ModelEntry) -> None:
        """Kick off a background scan_model_cache; update _strip_arch when done."""
        from hf_cache import scan_model_cache
        hf_repo = entry.hf_model_repo
        self._arch_scan_model = hf_repo

        def _run():
            info = scan_model_cache(hf_repo)
            GLib.idle_add(self._on_arch_scanned, hf_repo, info)

        threading.Thread(target=_run, daemon=True).start()

    def _on_arch_scanned(self, hf_repo: str, info) -> None:
        """Called on GTK thread after arch scan completes."""
        if self._entry is None or self._entry.hf_model_repo != hf_repo:
            return  # model changed while scan was in flight
        if not info.is_cached:
            return
        parts = []
        if info.arch:
            a = info.arch
            if a.num_layers:
                parts.append(f"{a.num_layers} layers")
            if a.num_heads:
                kv = f"/{a.num_kv_heads} KV" if a.num_kv_heads and a.num_kv_heads != a.num_heads else ""
                parts.append(f"{a.num_heads}{kv} heads")
            if a.context_length:
                parts.append(f"ctx {a.context_length:,}")
            if a.vocab_size:
                parts.append(f"vocab {a.vocab_size:,}")
        if info.total_bytes:
            gb = info.total_bytes / 1e9
            parts.append(f"{gb:.1f} GB cached")
        if parts:
            self._strip_arch.set_text("  ·  ".join(parts))
            self._strip_arch.set_visible(True)

    def set_compat_info(self, compat_entry) -> None:
        """Show hardware compatibility from the TT compat catalog."""
        if compat_entry is None:
            self._strip_compat.set_visible(False)
            return
        parts = []
        for c in compat_entry.compatibility:
            if "tt-inference-server" not in c.software:
                continue
            if c.status == "Not Supported":
                continue
            hw = c.hardware.upper()
            mark = "✓" if c.status == "Supported" else "⚠"
            parts.append(f"{hw} {mark}")
        if parts:
            self._strip_compat.set_text("Compat:  " + "  ·  ".join(parts[:6]))
            self._strip_compat.set_visible(True)
        else:
            self._strip_compat.set_visible(False)

    # ------------------------------------------------------------- use cases

    def _populate_use_cases(self, entry: ModelEntry) -> None:
        for child in list(self._uc_box):
            self._uc_box.remove(child)
        self._use_case_btns.clear()
        use_cases = MODEL_TYPE_USE_CASES.get(entry.model_type, ["dev"])
        for uc in use_cases:
            btn = Gtk.ToggleButton(label=USE_CASE_LABELS.get(uc, uc))
            btn.add_css_class("chip")
            btn.connect("toggled", self._on_uc_toggled, uc)
            self._uc_box.append(btn)
            self._use_case_btns[uc] = btn

    def _apply_use_case(self, use_case: str) -> None:
        """Set active chip and fill quick-settings fields from preset."""
        if self._entry is None:
            return
        self._inhibit_signals = True
        self._options = apply_preset(use_case, self._entry)

        # Highlight selected chip
        for uc, btn in self._use_case_btns.items():
            btn.handler_block_by_func(self._on_uc_toggled)
            btn.set_active(uc == use_case)
            btn.handler_unblock_by_func(self._on_uc_toggled)

        # Fill quick-settings widgets from options
        self._sync_widgets_to_options()
        self._inhibit_signals = False

    def _sync_widgets_to_options(self) -> None:
        """Push current self._options values into all widgets (no callbacks fired)."""
        # Context length
        ctx_entry = self._ctx_combo.get_child()
        if self._options.max_model_len is not None:
            ctx_entry.set_text(str(self._options.max_model_len))
        else:
            ctx_entry.set_text("default")

        # Max concurrent seqs
        seq_entry = self._seq_combo.get_child()
        if self._options.max_num_seqs is not None:
            seq_entry.set_text(str(self._options.max_num_seqs))
        else:
            seq_entry.set_text("default")

        # Tool use
        self._tool_toggle.set_active(self._options.tool_use_enabled)
        parser = self._options.tool_call_parser
        if parser and parser in _PARSERS:
            self._parser_combo.set_active(_PARSERS.index(parser))
        else:
            self._parser_combo.set_active(0)
        self._auto_tool_check.set_active(self._options.enable_auto_tool_choice)
        tool_detail_visible = self._options.tool_use_enabled
        self._parser_combo.set_visible(tool_detail_visible)
        self._auto_tool_check.set_visible(tool_detail_visible)

        # General
        self._dev_mode_check.set_active(self._options.dev_mode)
        self._no_timeout_check.set_active(self._options.disable_metal_timeout)
        self._workflow_entry.set_text(self._options.workflow_args)

        # Advanced
        self._vllm_extra_entry.set_text(self._options.extra_vllm_args)
        self._tt_config_entry.set_text(self._options.override_tt_config)
        self._hf_cache_entry.set_text(self._options.host_hf_cache)
        self._volume_entry.set_text(self._options.host_volume)
        self._weights_entry.set_text(self._options.host_weights_dir)
        self._bind_entry.set_text(self._options.bind_host)
        self._device_id_entry.set_text(self._options.device_id)
        self._image_user_entry.set_text(self._options.image_user)
        self._skip_sw_check.set_active(self._options.skip_system_sw_validation)
        self._no_trace_check.set_active(self._options.disable_trace_capture)

    # ----------------------------------------------------------- signal handlers

    def _on_uc_toggled(self, btn: Gtk.ToggleButton, use_case: str) -> None:
        if self._inhibit_signals or not btn.get_active():
            return
        self._apply_use_case(use_case)
        self._schedule_preview_update()
        self._on_options_changed(self._options)

    def _on_ctx_changed(self, entry: Gtk.Entry) -> None:
        if self._inhibit_signals:
            return
        text = entry.get_text().strip()
        if text == "default":
            self._options.max_model_len = None
        else:
            try:
                self._options.max_model_len = int(text)
            except ValueError:
                pass
        self._deselect_use_case_chip()
        self._schedule_preview_update()

    def _on_seq_changed(self, entry: Gtk.Entry) -> None:
        if self._inhibit_signals:
            return
        text = entry.get_text().strip()
        if text == "default":
            self._options.max_num_seqs = None
        else:
            try:
                self._options.max_num_seqs = int(text)
            except ValueError:
                pass
        self._deselect_use_case_chip()
        self._schedule_preview_update()

    def _on_tool_toggled(self, btn: Gtk.ToggleButton) -> None:
        if self._inhibit_signals:
            return
        enabled = btn.get_active()
        self._options.tool_use_enabled = enabled
        self._parser_combo.set_visible(enabled)
        self._auto_tool_check.set_visible(enabled)
        if enabled and self._entry and not self._options.tool_call_parser:
            self._options.tool_call_parser = detect_tool_parser(self._entry)
            idx = _PARSERS.index(self._options.tool_call_parser) if self._options.tool_call_parser in _PARSERS else 0
            self._parser_combo.set_active(idx)
        self._deselect_use_case_chip()
        self._schedule_preview_update()

    def _on_parser_changed(self, combo: Gtk.ComboBoxText) -> None:
        if self._inhibit_signals:
            return
        self._options.tool_call_parser = combo.get_active_text() or ""
        self._schedule_preview_update()

    def _on_any_change(self, widget) -> None:
        if self._inhibit_signals:
            return
        self._read_widgets_to_options()
        self._deselect_use_case_chip()
        self._schedule_preview_update()
        self._on_options_changed(self._options)

    def _on_vllm_extra_changed(self, entry: Gtk.Entry) -> None:
        if self._inhibit_signals:
            return
        text = entry.get_text().strip()
        self._options.extra_vllm_args = text
        # Validate JSON — show red border when invalid
        if text:
            try:
                json.loads(text)
                entry.remove_css_class("error")
            except json.JSONDecodeError:
                entry.add_css_class("error")
        else:
            entry.remove_css_class("error")
        self._schedule_preview_update()

    def _on_docker_changed(self, combo: Gtk.ComboBoxText) -> None:
        if self._inhibit_signals:
            return
        active_id = combo.get_active_id()
        if active_id == "__spec__":
            self._options.docker_image_override = ""
        else:
            # active_id is the repo_tag for non-spec entries
            self._options.docker_image_override = active_id or ""
        self._schedule_preview_update()

    def _on_docker_refresh(self, _btn) -> None:
        if self._entry:
            self._refresh_docker_images(self._entry.docker_image)

    # --------------------------------------------------------- docker images

    def _refresh_docker_images(self, spec_default: str = "") -> None:
        self._docker_status.set_text("scanning…")
        def _scan():
            images = scan_local_images(spec_default)
            idle_add_once(self._populate_docker_combo, images, spec_default)
        threading.Thread(target=_scan, daemon=True).start()

    def _populate_docker_combo(
        self, images: List[DockerImage], spec_default: str
    ) -> None:
        self._docker_images = images
        self._inhibit_signals = True
        # Remove all but the placeholder
        while self._docker_combo.get_model().iter_n_children(None) > 1:
            self._docker_combo.remove(1)
        for img in images:
            if img.repo_tag == spec_default:
                label = f"{img.short_tag}  ·  {img.size_str}  ·  {img.created_str}  (spec default)"
            else:
                label = f"{img.short_tag}  ·  {img.size_str}  ·  {img.created_str}"
            self._docker_combo.append(img.repo_tag, label)
        # Select spec-default entry if present
        if images and images[0].repo_tag == spec_default:
            self._docker_combo.set_active(1)
            pulled = images[0].created_str != "not pulled"
            self._docker_status.set_text("✓ pulled" if pulled else "✗ not pulled")
        else:
            self._docker_combo.set_active(0)
            self._docker_status.set_text(f"{len(images)} local images")
        self._inhibit_signals = False

    # ------------------------------------------------------------ profiles

    def _refresh_profiles(self, model_type: str) -> None:
        self._inhibit_signals = True
        while self._profile_combo.get_model().iter_n_children(None) > 1:
            self._profile_combo.remove(1)
        for p in list_profiles(model_type):
            name = p.get("name", "")
            desc = p.get("description", "")
            label = f"{name}  —  {desc}" if desc else name
            self._profile_combo.append(name, label)
        self._profile_combo.set_active(0)
        self._del_profile_btn.set_sensitive(False)
        self._inhibit_signals = False

    def _on_profile_selected(self, combo: Gtk.ComboBoxText) -> None:
        if self._inhibit_signals:
            return
        active_id = combo.get_active_id()
        if active_id == "__none__" or not active_id:
            self._del_profile_btn.set_sensitive(False)
            return
        self._del_profile_btn.set_sensitive(True)
        from profiles import load_profile
        p = load_profile(active_id)
        if p and self._entry:
            self._inhibit_signals = True
            self._options = profile_to_options(p)
            self._options.use_case = p.get("options", {}).get("use_case", "chat")
            self._sync_widgets_to_options()
            # Highlight matching chip if any
            for uc, btn in self._use_case_btns.items():
                btn.handler_block_by_func(self._on_uc_toggled)
                btn.set_active(uc == self._options.use_case)
                btn.handler_unblock_by_func(self._on_uc_toggled)
            self._inhibit_signals = False
            self._update_preview()
            self._on_options_changed(self._options)

    def _on_save_profile(self, _btn) -> None:
        if self._entry is None:
            return
        dialog = Gtk.Dialog(title="Save Profile", transient_for=self.get_root(), modal=True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Save", Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(6)
        box.set_margin_start(12); box.set_margin_end(12)
        box.set_margin_top(8);   box.set_margin_bottom(8)
        name_entry = Gtk.Entry()
        name_entry.set_placeholder_text("Profile name (no spaces)")
        box.append(Gtk.Label(label="Name:"))
        box.append(name_entry)
        desc_entry = Gtk.Entry()
        desc_entry.set_placeholder_text("Optional description")
        box.append(Gtk.Label(label="Description:"))
        box.append(desc_entry)
        dialog.present()

        def _on_response(dlg, resp):
            if resp == Gtk.ResponseType.OK:
                name = name_entry.get_text().strip().replace(" ", "-")
                desc = desc_entry.get_text().strip()
                if name and self._entry:
                    save_profile(name, desc, self._entry.model_type, self._options)
                    self._refresh_profiles(self._entry.model_type)
            dlg.destroy()

        dialog.connect("response", _on_response)

    def _on_delete_profile(self, _btn) -> None:
        active_id = self._profile_combo.get_active_id()
        if not active_id or active_id == "__none__":
            return
        delete_profile(active_id)
        if self._entry:
            self._refresh_profiles(self._entry.model_type)

    # ---------------------------------------------------------- options sync

    def _read_widgets_to_options(self) -> None:
        """Sync all editable widget values into self._options."""
        opts = self._options
        opts.dev_mode = self._dev_mode_check.get_active()
        opts.disable_metal_timeout = self._no_timeout_check.get_active()
        opts.workflow_args = self._workflow_entry.get_text().strip()
        opts.extra_vllm_args = self._vllm_extra_entry.get_text().strip()
        opts.override_tt_config = self._tt_config_entry.get_text().strip()
        opts.host_hf_cache = self._hf_cache_entry.get_text().strip()
        opts.host_volume = self._volume_entry.get_text().strip()
        opts.host_weights_dir = self._weights_entry.get_text().strip()
        opts.bind_host = self._bind_entry.get_text().strip()
        opts.device_id = self._device_id_entry.get_text().strip()
        opts.image_user = self._image_user_entry.get_text().strip()
        opts.skip_system_sw_validation = self._skip_sw_check.get_active()
        opts.disable_trace_capture = self._no_trace_check.get_active()
        opts.tool_use_enabled = self._tool_toggle.get_active()
        opts.enable_auto_tool_choice = self._auto_tool_check.get_active()
        opts.tool_call_parser = self._parser_combo.get_active_text() or ""

    def _deselect_use_case_chip(self) -> None:
        """Deselect all use-case chips (user edited a field that contradicts the preset)."""
        for btn in self._use_case_btns.values():
            if btn.get_active():
                btn.handler_block_by_func(self._on_uc_toggled)
                btn.set_active(False)
                btn.handler_unblock_by_func(self._on_uc_toggled)

    def get_options(self) -> LaunchOptions:
        return self._options

    # -------------------------------------------------------- command preview

    def _schedule_preview_update(self) -> None:
        if self._preview_source:
            GLib.source_remove(self._preview_source)
        self._preview_source = GLib.timeout_add(150, self._update_preview_cb)

    def _update_preview_cb(self) -> bool:
        self._preview_source = None
        self._update_preview()
        return False

    def _update_preview(self) -> None:
        if self._entry is None:
            self._preview_buf.set_text("")
            return
        e = self._entry
        base = [
            "python3 run.py",
            f"--model {e.display_name}",
            "--workflow server --docker-server",
            f"--service-port 8000",
            f"--tt-device {e.device_type.lower()}",
            "--no-auth",
        ]
        if self._options.docker_image_override:
            base.append(f"--override-docker-image {self._options.docker_image_override}")

        class _E:
            inference_engine = e.inference_engine
            family = e.family
        extra = build_extra_args(self._options, _E())
        all_parts = base + extra
        self._preview_buf.set_text(" \\\n  ".join(all_parts))
