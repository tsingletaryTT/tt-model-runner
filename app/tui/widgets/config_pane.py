#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""ConfigPane — Config tab for the Textual TUI."""
from __future__ import annotations

from typing import Callable, Optional, TYPE_CHECKING

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Input, Label, Static

if TYPE_CHECKING:
    from model_catalog import ModelEntry
    from launch_options import LaunchOptions


_USE_CASE_LABELS = {
    "chat":              "Chat",
    "code_completion":   "Code",
    "agent_frameworks":  "Agent",
    "deep_research":     "Research",
    "creative_writing":  "Creative",
    "dev":               "Dev",
    "smoke_test":        "Smoke",
}

# (checkbox widget id, LaunchOptions field name)
_CHECKBOXES = [
    ("tool-use-check",  "tool_use_enabled"),
    ("dev-mode-check",  "dev_mode"),
    ("no-timeout-check","disable_metal_timeout"),
    ("skip-sw-check",   "skip_system_sw_validation"),
    ("no-trace-check",  "disable_trace_capture"),
]


class ConfigPane(Widget):
    """Config tab: use-case selector, quick settings, command preview."""

    DEFAULT_CSS = """
    ConfigPane {
        height: 100%;
        layout: vertical;
        padding: 0 1;
    }
    #model-strip {
        height: 2;
        color: $text-muted;
    }
    #arch-strip {
        color: $text-muted;
        height: auto;
    }
    #desc-strip {
        color: $text-muted;
        height: auto;
    }
    #compat-strip {
        color: $text-muted;
        height: auto;
    }
    #use-case-row {
        height: 3;
        layout: horizontal;
    }
    #quick-settings {
        layout: vertical;
        height: auto;
    }
    #command-preview {
        height: 1fr;
        border: solid $primary-darken-2;
        padding: 0 1;
        color: $text-muted;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._entry: Optional["ModelEntry"] = None
        self._options = None
        self._on_options_changed: Optional[Callable] = None
        self._syncing: bool = False   # suppress change callbacks during sync
        self._on_dev_launch: Optional[Callable] = None  # (model_id, sw_stack) → None
        self._dev_stacks: list = []   # ["tt-forge", "tt-metal", ...] for current model
        self._arch_scan_model: str = ""  # hf_model_repo of in-flight arch scan

    def compose(self) -> ComposeResult:
        yield Static("Select a model to configure", id="model-strip")
        yield Static("", id="arch-strip")
        yield Static("", id="desc-strip")
        yield Static("", id="compat-strip")
        yield Label("USE CASE")
        yield Widget(id="use-case-row")
        yield Label("QUICK SETTINGS")
        with Widget(id="quick-settings"):
            yield Input(placeholder="Context length (e.g. 131072)", id="ctx-input")
            yield Input(placeholder="Max concurrent seqs (e.g. 1)", id="seq-input")
            yield Checkbox("Enable tool use",         id="tool-use-check")
            yield Checkbox("Dev mode",                id="dev-mode-check")
            yield Checkbox("Disable TT timeout",      id="no-timeout-check")
            yield Checkbox("Skip SW validation",      id="skip-sw-check")
            yield Checkbox("Disable trace capture",   id="no-trace-check")
        yield Label("COMMAND PREVIEW")
        yield Static("", id="command-preview")
        yield Static("", id="dev-image-strip")
        yield Widget(id="dev-image-row")

    def set_model(self, entry, on_options_changed: Callable) -> None:
        """Update ConfigPane for a newly selected model entry."""
        from launch_options import LaunchOptions, MODEL_TYPE_USE_CASES, apply_preset

        self._entry = entry
        self._on_options_changed = on_options_changed

        self.query_one("#model-strip", Static).update(
            f"[b]{entry.display_name}[/b]  {entry.model_type} · {entry.inference_engine} · {entry.device_type}"
        )
        # Clear derived info until callbacks populate it
        self.query_one("#arch-strip", Static).update("")
        self.query_one("#desc-strip", Static).update("")
        self.query_one("#compat-strip", Static).update("")
        self._scan_arch_async(entry)

        row = self.query_one("#use-case-row")
        row.remove_children()
        use_cases = MODEL_TYPE_USE_CASES.get(entry.model_type, ["dev"])
        for uc in use_cases:
            label = _USE_CASE_LABELS.get(uc, uc)
            btn = Button(label, id=f"uc-{uc}", variant="default")
            row.mount(btn)

        default_uc = use_cases[0]
        self._options = apply_preset(default_uc, entry)
        self._sync_widgets_to_options()
        if self._on_options_changed and self._options:
            self._on_options_changed(self._options)
        self._update_preview()

    def set_compat_info(self, compat_entry, compatible_hw: list) -> None:
        """Show description, hardware compatibility, and dev image buttons from compat catalog."""
        if compat_entry and compat_entry.model_description:
            self.query_one("#desc-strip", Static).update(compat_entry.model_description)
        else:
            self.query_one("#desc-strip", Static).update("")

        if compat_entry:
            hw_parts = []
            for c in compat_entry.compatibility:
                if c.status != "Not Supported":
                    marker = "✓" if c.status == "Supported" else "~"
                    hw_parts.append(f"{c.hardware.upper()} {marker}")
            if hw_parts:
                self.query_one("#compat-strip", Static).update("Compat: " + "  ·  ".join(hw_parts))
            else:
                self.query_one("#compat-strip", Static).update("")

            # Collect dev image stacks for this model
            self._dev_stacks = []
            for c in compat_entry.compatibility:
                for sw in ("tt-forge", "tt-metal"):
                    if sw in c.software and sw not in self._dev_stacks:
                        self._dev_stacks.append(sw)

            row = self.query_one("#dev-image-row")
            row.remove_children()
            if self._dev_stacks:
                self.query_one("#dev-image-strip", Static).update("ALSO VIA DEVELOPER IMAGE")
                for sw in self._dev_stacks:
                    btn = Button(f"▶ {sw}", id=f"dev-{sw.replace('-', '_')}")
                    row.mount(btn)
            else:
                self.query_one("#dev-image-strip", Static).update("")
        else:
            self.query_one("#compat-strip", Static).update("")
            self.query_one("#dev-image-strip", Static).update("")
            self.query_one("#dev-image-row").remove_children()
            self._dev_stacks = []

    def set_dev_launch_callback(self, callback) -> None:
        self._on_dev_launch = callback

    # ---------------------------------------------------------------- arch scan

    def _scan_arch_async(self, entry) -> None:
        """Background HF cache scan; surfaces arch facts in #arch-strip when done."""
        import threading
        from hf_cache import scan_model_cache
        hf_repo = getattr(entry, "hf_model_repo", None)
        if not hf_repo:
            return
        self._arch_scan_model = hf_repo

        def _run():
            info = scan_model_cache(hf_repo)
            self.app.call_from_thread(self._on_arch_scanned, hf_repo, info)

        threading.Thread(target=_run, daemon=True).start()

    def _on_arch_scanned(self, hf_repo: str, info) -> None:
        if self._entry is None or getattr(self._entry, "hf_model_repo", "") != hf_repo:
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
            self.query_one("#arch-strip", Static).update("  ·  ".join(parts))

    # ---------------------------------------------------------------- event handlers

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        if btn_id.startswith("uc-"):
            from launch_options import apply_preset
            uc = btn_id[3:]
            if self._entry:
                self._options = apply_preset(uc, self._entry)
                self._sync_widgets_to_options()
                self._update_preview()
                if self._on_options_changed and self._options:
                    self._on_options_changed(self._options)
        elif btn_id.startswith("dev-") and self._on_dev_launch and self._entry:
            sw = btn_id[4:].replace("_", "-")
            self._on_dev_launch(self._entry.display_name.lower().replace(" ", "-"), sw)

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._syncing or not self._options:
            return
        inp_id = event.input.id
        text = event.value.strip()
        if inp_id == "ctx-input":
            if not text or text.lower() == "default":
                self._options.max_model_len = None
            else:
                try:
                    self._options.max_model_len = int(text)
                except ValueError:
                    return
        elif inp_id == "seq-input":
            if not text or text.lower() == "default":
                self._options.max_num_seqs = None
            else:
                try:
                    self._options.max_num_seqs = int(text)
                except ValueError:
                    return
        else:
            return
        self._update_preview()
        if self._on_options_changed:
            self._on_options_changed(self._options)

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if self._syncing or not self._options:
            return
        cb_id = event.checkbox.id
        val = event.value
        for widget_id, attr in _CHECKBOXES:
            if cb_id == widget_id:
                setattr(self._options, attr, val)
                break
        self._update_preview()
        if self._on_options_changed:
            self._on_options_changed(self._options)

    # ---------------------------------------------------------------- sync helpers

    def _sync_widgets_to_options(self) -> None:
        """Push self._options values into all input widgets without firing callbacks."""
        if not self._options:
            return
        self._syncing = True
        try:
            # Numeric inputs
            for inp_id, attr in [("ctx-input", "max_model_len"), ("seq-input", "max_num_seqs")]:
                try:
                    val = getattr(self._options, attr, None)
                    self.query_one(f"#{inp_id}", Input).value = str(val) if val is not None else ""
                except Exception:
                    pass
            # Checkboxes
            for cb_id, attr in _CHECKBOXES:
                try:
                    self.query_one(f"#{cb_id}", Checkbox).value = bool(
                        getattr(self._options, attr, False)
                    )
                except Exception:
                    pass
        finally:
            self._syncing = False

    # ---------------------------------------------------------------- preview

    def _update_preview(self) -> None:
        if not self._entry or not self._options:
            return
        from launch_options import build_extra_args

        e = self._entry
        parts = [
            "python3 run.py",
            f"--model {e.display_name}",
            "--workflow server --docker-server",
            "--service-port 8000",
            f"--tt-device {e.device_type.lower()}",
            "--no-auth",
        ]

        class _E:
            inference_engine = e.inference_engine
            family = e.family

        parts += build_extra_args(self._options, _E())
        self.query_one("#command-preview", Static).update(
            " \\\n  ".join(parts)
        )
