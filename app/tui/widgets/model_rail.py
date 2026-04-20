#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""ModelRail — collapsible left sidebar for the Textual TUI."""
from __future__ import annotations

from typing import Callable, List, Optional, TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Button, Input, Label, ListItem, ListView, Static

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

    BINDINGS = [
        Binding("s", "toggle_star", "Star", show=False),
    ]

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
        self._catalog = None

    def compose(self) -> ComposeResult:
        yield Static("[b]TT Model Runner[/b]", markup=True)
        yield Static("", id="state-pill")
        yield Static("", id="starred-label", markup=True)
        yield ListView(id="starred-list")
        yield Static("", id="recent-label", markup=True)
        yield ListView(id="recent-list")
        yield Label("Model:", classes="rail-section-label")
        yield ListView(id="model-list")
        yield Static("", id="discover-label")
        yield ListView(id="discover-list")
        yield Label("Port:", classes="rail-section-label")
        yield Input(value="8000", id="port-input", placeholder="8000")
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
        self._refresh_starred_recent()

    def _refresh_starred_recent(self) -> None:
        """Re-populate the STARRED and RECENT sections from settings."""
        try:
            from app_settings import settings as _settings
        except Exception:
            return
        if not self._catalog:
            return

        # STARRED
        starred_recs = _settings.starred_models or []
        starred_entries = []
        for rec in starred_recs:
            e = self._catalog.get_entry(rec.get("model_name", ""), rec.get("device", ""))
            if e:
                starred_entries.append(e)
        slv = self.query_one("#starred-list", ListView)
        slv.clear()
        slbl = self.query_one("#starred-label", Static)
        if starred_entries:
            slbl.update(f"[yellow]★ STARRED ({len(starred_entries)})[/yellow]")
            for e in starred_entries:
                item = ListItem(Label(f"★ {e.display_name[:16]}\n  {e.device_type}"))
                item._entry = e
                item._compat_entry = None
                slv.append(item)
        else:
            slbl.update("")

        # RECENT (up to 3)
        recent_recs = (_settings.recent_models or [])[:3]
        recent_entries = []
        for rec in recent_recs:
            e = self._catalog.get_entry(rec.get("model_name", ""), rec.get("device", ""))
            if e:
                recent_entries.append(e)
        rlv = self.query_one("#recent-list", ListView)
        rlv.clear()
        rlbl = self.query_one("#recent-label", Static)
        if recent_entries:
            rlbl.update(f"[dim]RECENT ({len(recent_entries)})[/dim]")
            for e in recent_entries:
                item = ListItem(Label(f"{e.display_name[:17]}\n  {e.device_type}"))
                item._entry = e
                item._compat_entry = None
                rlv.append(item)
        else:
            rlbl.update("")

    def load_compat_catalog(self, catalog, device_type: Optional[str] = None) -> None:
        """Add compat-only DISCOVER entries below the main model list."""
        known_names = {e.display_name.lower() for e in self._entries}
        self._compat_entries = []
        if device_type:
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

    def action_toggle_star(self) -> None:
        """Toggle star on the currently selected model entry."""
        if not self.selected_entry:
            self.app.notify("Select a model to star/unstar", severity="warning")
            return
        app_ctrl = getattr(self.app, "_ctrl", None)
        if not app_ctrl:
            return
        is_now_starred = app_ctrl.toggle_star(self.selected_entry)
        name = self.selected_entry.display_name
        if is_now_starred:
            self.app.notify(f"★ Starred: {name}")
        else:
            self.app.notify(f"Unstarred: {name}")
        self._refresh_starred_recent()

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

    def set_port(self, port: str) -> None:
        """Update the port input and cached value (called from TuiApp.on_mount)."""
        self.port_value = port or "8000"
        try:
            self.query_one("#port-input", Input).value = self.port_value
        except Exception:
            pass

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "port-input":
            val = event.value.strip()
            if val.isdigit() and 1 <= int(val) <= 65535:
                self.port_value = val
                # Persist so the GTK app sees the same port next time.
                try:
                    from app_settings import settings as _settings
                    _settings.last_port = val
                    _settings.save()
                except Exception:
                    pass

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
