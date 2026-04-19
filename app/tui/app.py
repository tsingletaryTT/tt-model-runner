#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""TuiApp — Textual Application for tt-model-runner-gui.

Creates AppController with call_from_thread as dispatch_fn so all on_*
callbacks are safely posted to the Textual event loop.
"""
import sys
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, TabbedContent, TabPane

sys.path.insert(0, str(Path(__file__).parent.parent))

from tui.widgets.model_rail import ModelRail
from tui.widgets.log_pane   import LogPane
from tui.widgets.config_pane import ConfigPane
from tui.widgets.tool_pane  import ToolPane
from tui.widgets.bench_pane import BenchPane


class TuiApp(App[None]):
    """Feature-equivalent TUI sharing AppController with the GTK GUI."""

    CSS = """
    Screen {
        layout: horizontal;
    }
    ModelRail {
        width: 22;
        background: $surface;
        border-right: solid $primary;
    }
    TabbedContent {
        width: 1fr;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("l", "launch_stop", "Launch/Stop"),
        Binding("1", "switch_tab('config')",  "Config",  show=False),
        Binding("2", "switch_tab('logs')",    "Logs",    show=False),
        Binding("3", "switch_tab('tools')",   "Tools",   show=False),
        Binding("4", "switch_tab('bench')",   "Bench",   show=False),
        Binding("[", "toggle_rail",            "Rail",    show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield ModelRail(id="rail")
        with TabbedContent(initial="config", id="tabs"):
            with TabPane("Config", id="config"):
                yield ConfigPane()
            with TabPane("Logs",   id="logs"):
                yield LogPane()
            with TabPane("Tools",  id="tools"):
                yield ToolPane()
            with TabPane("Bench",  id="bench"):
                yield BenchPane()
        yield Footer()

    def on_mount(self) -> None:
        """Create AppController and register all view callbacks."""
        import threading
        from controller import AppController
        from app_settings import settings as _settings

        _main_thread_id = threading.get_ident()
        self._ctrl = AppController(
            dispatch_fn=lambda fn, *a: fn(*a) if threading.get_ident() == _main_thread_id
            else self.call_from_thread(fn, *a)
        )

        self._ctrl.on_state_changed   = self._on_state_changed
        self._ctrl.on_log_line        = self._on_log_line
        self._ctrl.on_progress        = self._on_progress
        self._ctrl.on_substage        = self._on_substage
        self._ctrl.on_catalog_loaded  = self._on_catalog_loaded
        self._ctrl.on_cache_scanned   = lambda _info: None
        self._ctrl.on_bench_progress  = self._on_bench_progress
        self._ctrl.on_bench_result    = self._on_bench_result
        self._ctrl.on_tool_result     = self._on_tool_result
        self._ctrl.on_hardware_status = self._on_hardware_status
        self._ctrl.on_running_servers = self._on_running_servers

        self._set_ready_tabs_enabled(False)

        rail = self.query_one(ModelRail)
        rail.on_launch = self._do_launch
        rail.on_stop   = lambda: self._ctrl.stop()

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
                if (candidate / "run.py").exists():
                    repo_path = candidate
                    break
        if repo_path:
            self.call_after_refresh(lambda: self._ctrl.load_repo(repo_path))

    def _on_state_changed(self, state, info: str) -> None:
        from server_manager import ServerState
        rail     = self.query_one(ModelRail)
        log_pane = self.query_one(LogPane)

        rail.update_state(state, info)
        log_pane.update_state(state, info)

        ready = (state == ServerState.READY)
        self._set_ready_tabs_enabled(ready)
        if state.name in ("LAUNCHING",):
            self.query_one(TabbedContent).active = "logs"
        elif ready:
            self.query_one(TabbedContent).active = "logs"

    def _on_log_line(self, line: str) -> None:
        self.query_one(LogPane).append_line(line)

    def _on_progress(self, fraction: float, label: str) -> None:
        self.query_one(LogPane).update_progress(fraction, label)

    def _on_substage(self, stepper: str, left: str, right: str, dots: str) -> None:
        self.query_one(LogPane).update_substage(stepper, left, right, dots)

    def _on_catalog_loaded(self, catalog, compatible_devices: list) -> None:
        rail = self.query_one(ModelRail)
        rail.load_catalog(catalog, compatible_devices)
        rail.on_model_select = self._on_model_select

    def _on_model_select(self, entry) -> None:
        self._ctrl.select_model(entry)
        config_pane = self.query_one(ConfigPane)
        config_pane.set_model(entry, self._on_options_changed)

    def _on_options_changed(self, options) -> None:
        self._ctrl.set_options(options)

    def _on_bench_progress(self, line: str) -> None:
        self.query_one(BenchPane).append_progress(line)

    def _on_bench_result(self, result) -> None:
        self.query_one(BenchPane).append_result(result)

    def _on_tool_result(self, rt) -> None:
        self.query_one(ToolPane).append_round_trip(rt)

    def action_launch_stop(self) -> None:
        from server_manager import ServerState
        if self._ctrl.state in (ServerState.IDLE, ServerState.ERROR):
            self._do_launch_from_rail()
        else:
            self._ctrl.stop()

    def _do_launch_from_rail(self) -> None:
        rail = self.query_one(ModelRail)
        entry = rail.selected_entry
        port  = rail.port_value
        if entry:
            opts = self._ctrl.get_options()
            self._ctrl.launch(entry, port, opts)

    def _do_launch(self, entry, port: str) -> None:
        opts = self._ctrl.get_options()
        self._ctrl.launch(entry, port, opts)

    def action_switch_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    def action_toggle_rail(self) -> None:
        self.query_one(ModelRail).toggle_class("collapsed")

    def _set_ready_tabs_enabled(self, enabled: bool) -> None:
        tabs = self.query_one(TabbedContent)
        for tab_id in ("tools", "bench"):
            tab = tabs.get_tab(tab_id)
            if tab is not None:
                tab.disabled = not enabled

    def _on_hardware_status(self, chips: list) -> None:
        """Show chip telemetry summary as an info log line."""
        if not chips:
            return
        parts = []
        for c in chips:
            temp = f"{c.temp_c:.0f}°C" if c.temp_c is not None else "?"
            clk  = f"{c.aiclk_mhz}MHz" if c.aiclk_mhz else ""
            parts.append(f"#{c.index} {c.board_type}  {temp}  {clk}".strip())
        self.query_one(LogPane).append_line("HW  " + "  |  ".join(parts))

    def _on_running_servers(self, servers: list) -> None:
        """Log a reconnect hint when a running TT server is detected on startup."""
        for s in servers:
            self.query_one(LogPane).append_line(
                f"⚡ Detected running server: {s.container_name} on port {s.port}"
            )
