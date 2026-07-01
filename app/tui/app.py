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

from tui.widgets.model_rail  import ModelRail
from tui.widgets.log_pane    import LogPane
from tui.widgets.config_pane import ConfigPane
from tui.widgets.tool_pane   import ToolPane
from tui.widgets.bench_pane  import BenchPane
from tui.widgets.images_pane import ImagesPane


class TuiApp(App[None]):
    """Feature-equivalent TUI sharing AppController with the GTK GUI."""

    # Set in on_mount; None beforehand so action guards can check safely.
    _ctrl = None
    # Stores (port, container_name) when a running server is detected on startup.
    _pending_reconnect: "Optional[tuple]" = None
    # Two-press confirmation for tt-smi -r: stores monotonic timestamp of first press.
    _hw_reset_confirm_ts: float = 0.0

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
        height: 1fr;
    }
    TabPane {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("l", "launch_stop", "Launch/Stop"),
        Binding("1", "switch_tab('config')",  "Config",  show=False),
        Binding("2", "switch_tab('logs')",    "Logs",    show=False),
        Binding("3", "switch_tab('tools')",   "Tools",   show=False),
        Binding("4", "switch_tab('bench')",   "Bench",   show=False),
        Binding("5", "switch_tab('images')",  "Images",  show=False),
        Binding("[",       "toggle_rail",      "Rail",      show=False),
        Binding("r",       "reconnect",        "Reconnect", show=False),
        Binding("ctrl+r",  "restart_server",   "Restart",   show=False),
        Binding("ctrl+h",  "hw_refresh",       "HW",        show=False),
        Binding("ctrl+t",  "hw_reset",         "HW Reset",  show=False),
        Binding("ctrl+u",  "copy_curl",        "Curl",      show=False),
        Binding("ctrl+b",  "open_browser",     "Browser",   show=False),
        Binding("ctrl+g",  "git_pull",         "Git pull",  show=False),
        Binding("ctrl+l",  "copy_log",         "Copy log",  show=False),
        Binding("ctrl+p",  "load_prev_log",    "Prev log",  show=False),
        Binding("h",       "toggle_hw_filter", "HW filter", show=False),
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
            with TabPane("Images", id="images"):
                yield ImagesPane()
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
        self._ctrl.on_compat_catalog_loaded = self._on_compat_catalog_loaded
        self._ctrl.on_docker_images = self._on_docker_images
        self._ctrl.on_model_identified = self._on_model_identified
        self._ctrl.on_download_progress = self._on_download_progress
        self._ctrl.on_environment_checked = self._on_environment_checked
        self._ctrl.on_remediation_applied = self._on_remediation_applied

        self._set_ready_tabs_enabled(False)

        rail = self.query_one(ModelRail)
        rail.on_launch = self._do_launch
        rail.on_stop   = lambda: self._ctrl.stop()
        # Pre-populate port from persisted settings.
        rail.set_port(_settings.last_port)

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

        # Pre-populate bench history from persisted data.
        self.call_after_refresh(self._load_bench_history)
        # Scan Docker images on startup.
        self.call_after_refresh(self._ctrl.scan_docker_images_async)

    def _on_state_changed(self, state, info: str) -> None:
        from server_manager import ServerState
        rail     = self.query_one(ModelRail)
        log_pane = self.query_one(LogPane)

        rail.update_state(state, info)
        log_pane.update_state(state, info)

        # Update app title to reflect active model and state.
        entry = self._ctrl.current_entry
        label = state.name
        if entry and state not in (ServerState.IDLE, ServerState.STOPPING):
            self.title = f"TT Model Runner — {entry.display_name} [{label}]"
        else:
            self.title = "TT Model Runner"

        ready = (state == ServerState.READY)
        self._set_ready_tabs_enabled(ready)
        if ready:
            # Re-enable the bench run button now that server is READY.
            self.query_one(BenchPane).set_running(False)
        if state.name in ("LAUNCHING", "RUNNING"):
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
        rail.on_compat_select = self._on_compat_select
        # If compat catalog already loaded, populate DISCOVER section now.
        if self._ctrl.compat_catalog:
            device = getattr(self, "_detected_device", None)
            rail.load_compat_catalog(self._ctrl.compat_catalog, device)
        self._rebuild_ad_cards()

    def _on_compat_catalog_loaded(self, catalog) -> None:
        """Wire compat catalog into the model rail DISCOVER section."""
        device = getattr(self, "_detected_device", None)
        self.query_one(ModelRail).load_compat_catalog(catalog, device)
        self._rebuild_ad_cards()

    def _rebuild_ad_cards(self) -> None:
        """Rebuild the rotating card pool from the current catalog + compat catalog."""
        try:
            from ad_facts import get_all_cards
            device = getattr(self, "_detected_device", None)
            cards = get_all_cards(
                self._ctrl.catalog if self._ctrl else None,
                device,
                self._ctrl.compat_catalog if self._ctrl else None,
            )
            self.query_one(LogPane).set_ad_cards(cards)
        except Exception:
            pass

    def _on_compat_select(self, compat_entry) -> None:
        """Show compat catalog entry details in the ConfigPane."""
        config_pane = self.query_one(ConfigPane)
        config_pane.set_compat_info(compat_entry, [])

    def _on_options_changed(self, options) -> None:
        self._ctrl.set_options(options)

    def _load_bench_history(self) -> None:
        history = self._ctrl.get_bench_history()
        if history:
            self.query_one(BenchPane).load_history(history)

    def _on_bench_progress(self, line: str) -> None:
        if line == "§BENCH_DONE§":
            self.query_one(BenchPane).set_running(False)
            return
        self.query_one(BenchPane).append_progress(line)

    def _on_bench_result(self, result) -> None:
        self.query_one(BenchPane).append_result(result)

    def _on_tool_result(self, rt) -> None:
        self.query_one(ToolPane).append_round_trip(rt)

    def _on_docker_images(self, images: list) -> None:
        self.query_one(ImagesPane).load_images(images)
        try:
            self.query_one(ConfigPane).load_docker_images(images)
        except Exception:
            pass

    def _on_model_identified(self, entry) -> None:
        """Auto-select identified model in ModelRail after reconnect."""
        rail = self.query_one(ModelRail)
        rail.selected_entry = entry
        rail._update_model_active(entry)
        self._on_model_select(entry)
        self.notify(f"Identified: {entry.display_name}", title="Model detected")

    def _on_model_select(self, entry) -> None:
        self._ctrl.select_model(entry)
        config_pane = self.query_one(ConfigPane)
        config_pane.set_model(entry, self._on_options_changed)
        config_pane.set_dev_launch_callback(self._ctrl.launch_dev_image)
        config_pane.set_download_callback(self._ctrl.download_model)
        # Show compat info if catalog has been loaded
        compat = self._ctrl.compat_catalog
        if compat:
            compat_entry = (compat.lookup(entry.display_name.lower().replace(" ", "-"))
                            or compat.lookup_by_display_name(entry.display_name))
            config_pane.set_compat_info(compat_entry, [])
        # Refresh Docker image list for this model's spec default image.
        self._ctrl.scan_docker_images_async()

    def _on_environment_checked(self, results: list) -> None:
        """Notify the user if any prereq is missing; detailed lines already in Logs."""
        failing = [name for name, ok, _desc in results if not ok]
        if failing:
            self.notify(
                f"Prerequisites missing: {', '.join(failing)} — see Logs",
                severity="warning",
                title="Environment check",
                timeout=8,
            )

    def _on_remediation_applied(self, remedy) -> None:
        """Reflect an auto-applied workaround as a toast (banner streams via log)."""
        ref = getattr(remedy, "ref", "") or getattr(remedy, "id", "")
        self.notify(f"Auto-tuned for {ref}", title="Known issue", severity="warning")

    def _on_download_progress(self, hf_repo: str, fraction: float, status_line: str) -> None:
        from textual.widgets import Static, Button
        try:
            config_pane = self.query_one(ConfigPane)
            timing = config_pane.query_one("#timing-strip", Static)
            dl_btn = config_pane.query_one("#dl-btn", Button)
            if fraction < 0:
                timing.update(f"[red]Download failed[/red]")
                dl_btn.label = "⬇  Download failed — retry?"
                dl_btn.disabled = False
            elif fraction >= 1.0:
                timing.update("")
                dl_btn.label = "✓  Cached — re-download?"
                dl_btn.disabled = False
                self.notify(f"Download complete: {hf_repo}", title="Download")
            else:
                pct = int(fraction * 100)
                timing.update(f"[cyan]⬇ {pct}%  {status_line[:40]}[/cyan]")
        except Exception:
            pass

    def action_launch_stop(self) -> None:
        if not self._ctrl:
            return
        from server_manager import ServerState
        if self._ctrl.state in (ServerState.IDLE, ServerState.ERROR, ServerState.DONE):
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
        # Refresh RECENT section after launch so the new entry appears immediately.
        self.call_after_refresh(
            lambda: self.query_one(ModelRail)._refresh_starred_recent()
        )

    def action_switch_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    def action_toggle_rail(self) -> None:
        self.query_one(ModelRail).toggle_class("collapsed")

    def _set_ready_tabs_enabled(self, enabled: bool) -> None:
        # Tools requires a live server; bench history is always viewable.
        tabs = self.query_one(TabbedContent)
        for tab_id in ("tools",):
            tab = tabs.get_tab(tab_id)
            if tab is not None:
                tab.disabled = not enabled

    def _on_hardware_status(self, chips: list) -> None:
        """Show chip telemetry summary as an info log line."""
        if not chips:
            return
        # Track the detected device type for DISCOVER section filtering.
        device_type = getattr(chips[0], "device_type", None) if chips else None
        if device_type:
            self._detected_device = device_type
        parts = []
        for c in chips:
            temp = f"{c.temp_c:.0f}°C" if c.temp_c is not None else "?"
            clk  = f"{c.aiclk_mhz}MHz" if c.aiclk_mhz else ""
            parts.append(f"#{c.index} {c.board_type}  {temp}  {clk}".strip())
        self.query_one(LogPane).append_line("HW  " + "  |  ".join(parts))
        self.query_one(ModelRail).update_hardware(chips)

    def _on_running_servers(self, servers: list) -> None:
        """Store detected server and offer reconnect via [R] key."""
        from server_manager import ServerState
        if not servers or self._ctrl.state not in (ServerState.IDLE, ServerState.ERROR):
            return
        server = servers[0]
        port = server.port or "8000"
        self._pending_reconnect = (port, server.container_name)
        extra = f" (+ {len(servers) - 1} more)" if len(servers) > 1 else ""
        self.query_one(LogPane).append_line(
            f"⚡ Detected running server: {server.container_name}  port {port}"
            f"  {server.running_for}{extra}"
        )
        self.notify(
            f"Running server on port {port} — press [R] to reconnect",
            title="Server detected",
            timeout=10,
        )

    def action_reconnect(self) -> None:
        """Reconnect to the most recently detected running server."""
        if not self._ctrl:
            return
        from server_manager import ServerState
        if not self._pending_reconnect:
            self.notify("No running server detected", severity="warning")
            return
        if self._ctrl.state not in (ServerState.IDLE, ServerState.ERROR):
            self.notify("Cannot reconnect: server already active", severity="warning")
            return
        port, container_name = self._pending_reconnect
        self._pending_reconnect = None
        self.query_one(LogPane).append_line(f"⟳ Reconnecting to {container_name} on port {port}…")
        self._ctrl.adopt_running_server(port, container_name)

    def action_copy_curl(self) -> None:
        """Copy a test curl command for the running server to clipboard (Ctrl+U)."""
        if not self._ctrl:
            return
        from server_manager import ServerState
        if self._ctrl.state != ServerState.READY:
            self.notify("Server must be READY to copy curl", severity="warning")
            return
        rail = self.query_one(ModelRail)
        port = rail.port_value or "8000"
        entry = self._ctrl.current_entry
        model = entry.hf_model_repo if entry else "default"
        cmd = (
            f'curl http://localhost:{port}/v1/chat/completions \\\n'
            f'  -H "Content-Type: application/json" \\\n'
            f'  -d \'{{"model": "{model}", "messages": [{{"role": "user", "content": "Hello!"}}]}}\''
        )
        self.copy_to_clipboard(cmd)
        self.notify(f"Copied curl for port {port}", title="Clipboard")

    def action_open_browser(self) -> None:
        """Open the running server's API docs in the default browser (Ctrl+B)."""
        if not self._ctrl:
            return
        from server_manager import ServerState
        if self._ctrl.state != ServerState.READY:
            self.notify("Server must be READY to open in browser", severity="warning")
            return
        import webbrowser
        rail = self.query_one(ModelRail)
        port = rail.port_value or "8000"
        url = f"http://localhost:{port}/docs"
        webbrowser.open(url)
        self.notify(f"Opening {url}", title="Browser")

    def action_restart_server(self) -> None:
        """Restart the server with the same model and options (Ctrl+R)."""
        if not self._ctrl:
            return
        from server_manager import ServerState
        if self._ctrl.state not in (ServerState.READY, ServerState.ERROR):
            self.notify("Restart only available when server is READY or ERROR",
                        severity="warning")
            return
        self._ctrl.restart()

    def action_hw_refresh(self) -> None:
        """Refresh chip telemetry (tt-smi -s)."""
        if not self._ctrl:
            return
        self._ctrl.refresh_hardware_status()
        self.notify("Refreshing chip telemetry…", timeout=3)

    def action_copy_log(self) -> None:
        """Copy all visible log lines to clipboard (Ctrl+L)."""
        if not self._ctrl:
            return
        log_pane = self.query_one(LogPane)
        lines = [line for line, level in log_pane._all_lines
                 if log_pane._line_visible(line, level)]
        if not lines:
            self.notify("No log lines to copy", severity="warning")
            return
        self.copy_to_clipboard("\n".join(lines))
        self.notify(f"Copied {len(lines)} lines to clipboard", title="Log copied")

    def action_git_pull(self) -> None:
        """git pull the configured server repo (Ctrl+G)."""
        if not self._ctrl:
            return
        self.query_one(TabbedContent).active = "logs"

        def _on_done(success: bool, summary: str) -> None:
            if success:
                branch, sha = self._ctrl.get_repo_git_info()
                info = f"  [{branch} @{sha}]" if branch else ""
                self.notify(f"git pull complete{info}", title="Repo updated")
            else:
                self.notify(f"git pull failed: {summary}", severity="error", title="Git pull")

        self._ctrl.pull_repo(on_complete=_on_done)

    def action_load_prev_log(self) -> None:
        """Ctrl+P — pick a previous session log and load it into the log pane."""
        if not self._ctrl:
            return
        logs = self._ctrl.list_session_logs(max_count=8)
        if not logs:
            self.notify("No previous session logs found", title="Session Log")
            return
        # Load the most recent previous session into the log pane
        from textual.widgets import RichLog as _RL
        log_pane = self.query_one(LogPane)
        newest = logs[0]
        lines = self._ctrl.load_session_log(newest)
        try:
            log_pane.query_one("#log-output", _RL).clear()
        except Exception:
            pass
        for line in lines:
            log_pane.append_line(line)
        log_pane.append_line(f"── Loaded {newest.name} ({len(lines)} lines) ──")
        self.query_one(TabbedContent).active = "logs"
        self.notify(f"Loaded {newest.name}", title="Previous Session Log")

    def action_toggle_hw_filter(self) -> None:
        """Toggle hardware-compatible-only model filter ([H])."""
        self.query_one(ModelRail).action_toggle_hw_filter()

    def action_hw_reset(self) -> None:
        """Run tt-smi -r — requires two presses within 5 s to confirm."""
        if not self._ctrl:
            return
        import time
        now = time.monotonic()
        if now - self._hw_reset_confirm_ts < 5.0:
            self._hw_reset_confirm_ts = 0.0
            from server_manager import ServerState
            if self._ctrl.state not in (ServerState.IDLE, ServerState.ERROR, ServerState.DONE):
                self.notify("Stop the server before resetting hardware", severity="warning")
                return
            self.query_one(LogPane).append_line("⟳ Running tt-smi -r…")
            self._ctrl.reset_hardware()
        else:
            self._hw_reset_confirm_ts = now
            self.notify(
                "Press Ctrl+T again within 5s to confirm tt-smi -r reset",
                title="Hardware reset",
                severity="warning",
                timeout=5,
            )
