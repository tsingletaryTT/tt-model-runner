#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""BenchPane — Bench tab for the Textual TUI."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Button, Checkbox, DataTable, Label, RichLog, Select

_MODES = [("smoke-test", "smoke-test"), ("ci-nightly", "ci-nightly"),
          ("ci-long", "ci-long")]


class BenchPane(Widget):
    """Bench tab: run config, live output, results table."""

    DEFAULT_CSS = """
    BenchPane {
        height: 100%;
        layout: vertical;
        padding: 0 1;
    }
    #bench-config-row {
        height: 3;
        layout: horizontal;
    }
    #bench-live-log {
        height: 1fr;
        border: solid $primary-darken-2;
    }
    #bench-results {
        height: 10;
    }
    """

    def compose(self) -> ComposeResult:
        with Widget(id="bench-config-row"):
            yield Label("Mode: ")
            yield Select(_MODES, value="smoke-test", id="bench-mode")
            yield Checkbox("Concurrency sweeps", id="bench-sweeps")
            yield Checkbox("Percentile report",  id="bench-pct")
            yield Button("▶ Run Benchmark", id="bench-run-btn", variant="success")
        yield Label("LIVE OUTPUT")
        yield RichLog(id="bench-live-log", highlight=False, markup=False)
        yield Label("RESULTS")
        yield DataTable(id="bench-results")

    def on_mount(self) -> None:
        table = self.query_one("#bench-results", DataTable)
        table.add_columns("Pass", "ISL", "OSL", "Con", "TTFT ms",
                          "TPS", "E2EL ms", "Req/s", "Timestamp")

    def load_history(self, history: list) -> None:
        """Pre-populate results table from persisted history (newest first)."""
        table = self.query_one("#bench-results", DataTable)
        table.clear()
        for r in history:
            icon = {"PASS": "✓", "BELOW_TARGET": "⚠", "FAIL": "✗"}.get(r.get("tier_pass", ""), "?")
            table.add_row(
                f"{icon} {r.get('tier_pass', '?')}",
                str(r.get("isl", "")),
                str(r.get("osl", "")),
                str(r.get("concurrency", "")),
                f"{r.get('mean_ttft_ms', 0):.0f}",
                f"{r.get('mean_tps', 0):.1f}",
                f"{r.get('mean_e2el_ms', 0):.0f}",
                f"{r.get('request_throughput', 0):.2f}",
                r.get("timestamp", "")[:16],
            )

    def append_progress(self, line: str) -> None:
        self.query_one("#bench-live-log", RichLog).write(line)

    def append_result(self, result) -> None:
        icon = {"PASS": "✓", "BELOW_TARGET": "⚠", "FAIL": "✗"}.get(
            result.tier_pass, "?"
        )
        table = self.query_one("#bench-results", DataTable)
        table.add_row(
            f"{icon} {result.tier_pass}",
            str(result.isl),
            str(result.osl),
            str(result.concurrency),
            f"{result.mean_ttft_ms:.0f}",
            f"{result.mean_tps:.1f}",
            f"{result.mean_e2el_ms:.0f}",
            f"{result.request_throughput:.2f}",
            result.timestamp,
        )

    def set_running(self, running: bool) -> None:
        """Toggle running state: disable/re-enable button and update label."""
        from server_manager import ServerState
        btn = self.query_one("#bench-run-btn", Button)
        if running:
            btn.label = "⏳ Running…"
            btn.disabled = True
        else:
            btn.label = "▶ Run Benchmark"
            app_ctrl = getattr(self.app, "_ctrl", None)
            btn.disabled = (app_ctrl is None or
                            app_ctrl.state != ServerState.READY)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "bench-run-btn":
            return
        from server_manager import ServerState
        app_ctrl = getattr(self.app, "_ctrl", None)
        if not app_ctrl or app_ctrl.state != ServerState.READY:
            self.notify("Server must be READY to run benchmarks", severity="warning")
            return
        mode   = self.query_one("#bench-mode", Select).value or "smoke-test"
        sweeps = self.query_one("#bench-sweeps", Checkbox).value
        pct    = self.query_one("#bench-pct", Checkbox).value
        self.query_one("#bench-live-log", RichLog).clear()
        self.set_running(True)
        app_ctrl.run_benchmark(mode=mode, concurrency_sweeps=sweeps,
                               percentile_report=pct)
