#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""ModelRail — collapsible left sidebar for the Textual TUI."""
from __future__ import annotations

from typing import Callable, List, Optional, TYPE_CHECKING

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Button, Label, ListItem, ListView, Static

if TYPE_CHECKING:
    from model_catalog import ModelEntry
    from server_manager import ServerState


_STATE_PILLS = {
    "IDLE":          ("● IDLE",     "dim"),
    "LAUNCHING":     ("● LAUNCHING","yellow"),
    "PULLING_IMAGE": ("● PULLING",  "yellow"),
    "LOADING":       ("● LOADING",  "cyan"),
    "READY":         ("● READY",    "green"),
    "RUNNING":       ("● RUNNING",  "cyan"),
    "DONE":          ("● DONE",     "green"),
    "ERROR":         ("● ERROR",    "red"),
    "STOPPING":      ("● STOPPING", "yellow"),
}


def _fmt_size(param_count) -> str:
    if not param_count:
        return ""
    if param_count >= 1e12:
        return f"{param_count/1e12:.0f}T"
    if param_count >= 1e9:
        v = param_count / 1e9
        return f"{v:.0f}B" if v >= 10 else f"{v:.1f}B"
    if param_count >= 1e6:
        v = param_count / 1e6
        return f"{v:.0f}M" if v >= 10 else f"{v:.1f}M"
    return ""


class ModelRail(Widget):
    """Collapsible left rail. Width: 22 expanded, 4 collapsed."""

    DEFAULT_CSS = """
    ModelRail {
        width: 22;
        height: 100%;
        padding: 0 1;
    }
    ModelRail.collapsed {
        width: 4;
        padding: 0;
    }
    ModelRail > .rail-section-label {
        color: $text-muted;
        text-style: bold;
    }
    """

    on_launch: Optional[Callable] = None
    on_stop:   Optional[Callable] = None
    on_model_select: Optional[Callable] = None

    selected_entry: Optional["ModelEntry"] = None
    port_value: str = "8000"

    on_compat_select: Optional[Callable] = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Instance-level lists — avoids the mutable class-level default pitfall.
        self._entries: list = []
        self._compat_entries: list = []  # (display_name, compat_entry, sw_stack) tuples

    def compose(self) -> ComposeResult:
        yield Static("[b]TT Model Runner[/b]", markup=True)
        yield Static("", id="state-pill")
        yield Label("Model:", classes="rail-section-label")
        yield ListView(id="model-list")
        yield Static("", id="discover-label")
        yield ListView(id="discover-list")
        yield Label("Port:", classes="rail-section-label")
        yield Static("8000", id="port-display")
        yield Button("▶ Launch", id="launch-btn", variant="success")

    def load_catalog(self, catalog, compatible_devices: List[str]) -> None:
        """Populate the model list from the catalog."""
        if compatible_devices:
            self._entries = [e for e in catalog.all_entries()
                             if e.device_type in compatible_devices]
        else:
            self._entries = list(catalog.all_entries())
        self._catalog = catalog
        lv = self.query_one("#model-list", ListView)
        lv.clear()
        for entry in self._entries:
            size = _fmt_size(getattr(entry, "param_count", None))
            suffix = f"  {size}" if size else ""
            item = ListItem(Label(f"{entry.display_name}{suffix}\n  {entry.device_type}"))
            item._entry = entry
            item._compat_entry = None
            lv.append(item)

    def load_compat_catalog(self, catalog, device_type: Optional[str] = None) -> None:
        """Add compat-only DISCOVER entries below the main model list."""
        known_names = {e.display_name.lower() for e in self._entries}
        self._compat_entries = []
        if device_type:
            from compat_catalog import _HW_MAP
            for sw in ("tt-forge", "tt-metal"):
                for ce in catalog.get_for_hardware(device_type, software=sw):
                    if ce.display_name.lower() not in known_names:
                        self._compat_entries.append((ce.display_name, ce, sw))
        else:
            seen = set()
            for ce in catalog.all_entries():
                if ce.display_name.lower() not in known_names and ce.id not in seen:
                    seen.add(ce.id)
                    sw = ce.compatibility[0].software[0] if ce.compatibility and ce.compatibility[0].software else "?"
                    self._compat_entries.append((ce.display_name, ce, sw))

        dlv = self.query_one("#discover-list", ListView)
        dlv.clear()
        lbl = self.query_one("#discover-label", Static)
        if self._compat_entries:
            lbl.update(f"[dim]— DISCOVER ({len(self._compat_entries)}) —[/dim]")
            for name, ce, sw in self._compat_entries:
                short_sw = sw.replace("tt-", "")
                item = ListItem(Label(f"{name[:18]}\n  [{short_sw}]"))
                item._entry = None
                item._compat_entry = ce
                dlv.append(item)
        else:
            lbl.update("")

    def update_state(self, state, info: str) -> None:
        """Update the state pill and toggle the launch/stop button label."""
        pill_text, color = _STATE_PILLS.get(state.name, ("● ?", "dim"))
        self.query_one("#state-pill", Static).update(
            f"[{color}]{pill_text}[/{color}]"
        )
        btn = self.query_one("#launch-btn", Button)
        if state.name in ("IDLE", "ERROR", "DONE"):
            btn.label = "▶ Launch"
            btn.variant = "success"
        elif state.name == "STOPPING":
            btn.label = "■ Stopping…"
            btn.variant = "warning"
        else:
            btn.label = "■ Stop"
            btn.variant = "error"

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        entry = getattr(item, "_entry", None)
        compat_entry = getattr(item, "_compat_entry", None)
        if entry is not None:
            self.selected_entry = entry
            if self.on_model_select:
                self.on_model_select(entry)
        elif compat_entry is not None:
            if self.on_compat_select:
                self.on_compat_select(compat_entry)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "launch-btn":
            from server_manager import ServerState
            app_ctrl = getattr(self.app, "_ctrl", None)
            if app_ctrl and app_ctrl.state not in (ServerState.IDLE, ServerState.ERROR):
                if self.on_stop:
                    self.on_stop()
            else:
                if self.selected_entry and self.on_launch:
                    self.on_launch(self.selected_entry, self.port_value)
