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

    def compose(self) -> ComposeResult:
        yield Static("Select a model to configure", id="model-strip")
        yield Label("USE CASE")
        yield Widget(id="use-case-row")
        yield Label("QUICK SETTINGS")
        with Widget(id="quick-settings"):
            yield Input(placeholder="Context length (e.g. 131072)", id="ctx-input")
            yield Input(placeholder="Max concurrent seqs (e.g. 1)", id="seq-input")
            yield Checkbox("Enable tool use", id="tool-use-check")
        yield Label("COMMAND PREVIEW")
        yield Static("", id="command-preview")

    def set_model(self, entry, on_options_changed: Callable) -> None:
        """Update ConfigPane for a newly selected model entry."""
        from launch_options import LaunchOptions, MODEL_TYPE_USE_CASES, apply_preset

        self._entry = entry
        self._on_options_changed = on_options_changed

        self.query_one("#model-strip", Static).update(
            f"[b]{entry.display_name}[/b]  {entry.model_type} · {entry.inference_engine} · {entry.device_type}"
        )

        row = self.query_one("#use-case-row")
        row.remove_children()
        use_cases = MODEL_TYPE_USE_CASES.get(entry.model_type, ["dev"])
        for uc in use_cases:
            label = _USE_CASE_LABELS.get(uc, uc)
            btn = Button(label, id=f"uc-{uc}", variant="default")
            row.mount(btn)

        default_uc = use_cases[0]
        self._options = apply_preset(default_uc, entry)
        self._update_preview()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        if btn_id.startswith("uc-"):
            from launch_options import apply_preset
            uc = btn_id[3:]
            if self._entry:
                self._options = apply_preset(uc, self._entry)
                self._update_preview()
                if self._on_options_changed and self._options:
                    self._on_options_changed(self._options)

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
