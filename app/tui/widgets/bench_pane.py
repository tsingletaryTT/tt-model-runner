#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""BenchPane — Bench tab for the Textual TUI."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Button, Checkbox, DataTable, Label, RichLog, Select, Static

_MODES = [("smoke-test", "smoke-test"), ("ci-nightly", "ci-nightly"),
          ("ci-long", "ci-long")]

_PASS_ICON = {"PASS": "✓", "BELOW_TARGET": "⚠", "FAIL": "✗"}


class BenchPane(Widget):
    """Bench tab: run config, live output, results table with click-to-detail."""

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
    #bench-hist-row {
        height: 3;
        layout: horizontal;
    }
    #bench-stats {
        color: $text-muted;
        height: 1;
        padding: 0 1;
    }
    #bench-live-log {
        height: 1fr;
        border: solid $primary-darken-2;
    }
    #bench-results {
        height: 8;
    }
    #bench-detail {
        height: auto;
        display: none;
        border: solid $primary-darken-2;
        padding: 0 1;
        color: $text-muted;
        margin-top: 0;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Full history records (newest first) — used for both table rendering and detail view.
        self._full_rows: List[Dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        with Widget(id="bench-config-row"):
            yield Label("Mode: ")
            yield Select(_MODES, value="smoke-test", id="bench-mode")
            yield Checkbox("Concurrency sweeps", id="bench-sweeps")
            yield Checkbox("Percentile report",  id="bench-pct")
            yield Button("Run Benchmark", id="bench-run-btn", variant="success")
        with Widget(id="bench-hist-row"):
            yield Button("Export CSV",     id="bench-csv-btn",   variant="default")
            yield Button("Clear History",  id="bench-clear-btn", variant="warning")
        yield Label("", id="bench-stats", markup=True)
        yield Label("LIVE OUTPUT")
        yield RichLog(id="bench-live-log", highlight=False, markup=False)
        yield Label("RESULTS  [dim](click row for details)[/dim]", markup=True)
        yield DataTable(id="bench-results")
        yield Static("", id="bench-detail", markup=True)

    def on_mount(self) -> None:
        table = self.query_one("#bench-results", DataTable)
        table.add_columns("Pass", "ISL", "OSL", "Con", "TTFT ms",
                          "TPS", "E2EL ms", "Req/s", "Timestamp")

    # ── Public API ───────────────────────────────────────────────────────────

    def load_history(self, history: list) -> None:
        """Pre-populate results table from persisted history (newest first)."""
        self._full_rows = list(history)   # already newest-first from controller
        self._render_table()
        self._update_stats()

    def append_progress(self, line: str) -> None:
        self.query_one("#bench-live-log", RichLog).write(line)

    def append_result(self, result) -> None:
        """Add a fresh BenchResult to the top of the table (newest first)."""
        # Convert dataclass to plain dict so storage is uniform with history dicts.
        try:
            row = asdict(result)
        except TypeError:
            row = vars(result) if hasattr(result, "__dict__") else {}
        self._full_rows.insert(0, row)
        self._render_table()
        self._update_stats()
        # Show the fresh result's detail immediately.
        if self._full_rows:
            self._show_detail(self._full_rows[0])

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

    # ── Table rendering ──────────────────────────────────────────────────────

    def _render_table(self) -> None:
        """Rebuild the DataTable from _full_rows (newest first) and focus row 0."""
        table = self.query_one("#bench-results", DataTable)
        table.clear()
        for r in self._full_rows:
            icon = _PASS_ICON.get(r.get("tier_pass", ""), "?")
            table.add_row(
                f"{icon} {r.get('tier_pass', '?')}",
                str(r.get("isl", "")),
                str(r.get("osl", "")),
                str(r.get("concurrency", "")),
                f"{r.get('mean_ttft_ms', 0) or 0:.0f}",
                f"{r.get('mean_tps', 0) or 0:.1f}",
                f"{r.get('mean_e2el_ms', 0) or 0:.0f}",
                f"{r.get('request_throughput', 0) or 0:.2f}",
                (r.get("timestamp", "") or "")[:16],
            )
        # Move cursor to the newest row (row 0) so it's immediately visible.
        if self._full_rows:
            table.move_cursor(row=0)

    # ── Detail panel ─────────────────────────────────────────────────────────

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Show full metrics for the row the cursor lands on."""
        idx = event.cursor_row
        if idx is not None and 0 <= idx < len(self._full_rows):
            self._show_detail(self._full_rows[idx])

    def _show_detail(self, row: dict) -> None:
        model   = row.get("model_name", "?")
        device  = row.get("device", "")
        isl     = row.get("isl", "?")
        osl     = row.get("osl", "?")
        con     = row.get("concurrency", "?")
        ttft    = row.get("mean_ttft_ms", 0) or 0
        p95     = row.get("p95_ttft_ms") or 0
        tps     = row.get("mean_tps", 0) or 0
        decode  = row.get("tps_decode", 0) or 0
        e2el    = row.get("mean_e2el_ms", 0) or 0
        req_s   = row.get("request_throughput", 0) or 0
        verdict = row.get("tier_pass", "?")
        ts      = (row.get("timestamp", "") or "")[:19]

        icon  = _PASS_ICON.get(verdict, "?")
        color = {"PASS": "green", "BELOW_TARGET": "yellow", "FAIL": "red"}.get(verdict, "dim")
        lines = [
            f"[bold]{model}[/bold]  [dim]{device}[/dim]   [{color}]{icon} {verdict}[/{color}]  [dim]{ts}[/dim]",
            f"  ISL [bold]{isl}[/bold]  OSL [bold]{osl}[/bold]  Concurrency [bold]{con}[/bold]",
            f"  TTFT [bold]{ttft:.1f} ms[/bold]  p95 [dim]{p95:.1f} ms[/dim]"
            f"   TPS [bold]{tps:.2f}[/bold]  decode [dim]{decode:.2f}[/dim]",
            f"  E2EL [bold]{e2el:.1f} ms[/bold]   Req/s [bold]{req_s:.3f}[/bold]",
        ]
        detail = self.query_one("#bench-detail", Static)
        detail.update("\n".join(lines))
        detail.display = True

    # ── Event handlers ───────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "bench-run-btn":
            self._do_run()
        elif btn_id == "bench-csv-btn":
            self._do_export_csv()
        elif btn_id == "bench-clear-btn":
            self._do_clear_history()

    def _do_run(self) -> None:
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

    def _do_export_csv(self) -> None:
        import csv, datetime, io
        from pathlib import Path
        app_ctrl = getattr(self.app, "_ctrl", None)
        if not app_ctrl:
            return
        history = app_ctrl.get_bench_history()
        if not history:
            self.notify("No benchmark history to export", severity="warning")
            return
        fields = [
            "timestamp", "model_name", "device", "isl", "osl", "concurrency",
            "mean_ttft_ms", "p95_ttft_ms", "mean_tps", "tps_decode",
            "mean_e2el_ms", "request_throughput", "tier_pass",
        ]
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = Path.home() / f"tt-benchmarks-{ts}.csv"
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(history)
        try:
            out_path.write_text(out.getvalue())
            self.notify(f"Saved to {out_path}", title="CSV exported")
        except OSError as exc:
            self.notify(f"Export failed: {exc}", severity="error")

    def _do_clear_history(self) -> None:
        app_ctrl = getattr(self.app, "_ctrl", None)
        if not app_ctrl:
            return
        app_ctrl.clear_bench_history()
        self._full_rows = []
        self.query_one("#bench-results", DataTable).clear()
        self.query_one("#bench-detail", Static).display = False
        self._update_stats()
        self.notify("Benchmark history cleared")

    # ── Stats ─────────────────────────────────────────────────────────────────

    def _update_stats(self) -> None:
        """Recompute and display aggregate stats across all stored results."""
        try:
            stats_lbl = self.query_one("#bench-stats", Label)
        except Exception:
            return
        if not self._full_rows:
            stats_lbl.update("")
            return
        tps_vals  = [r.get("mean_tps", 0) or 0     for r in self._full_rows if (r.get("mean_tps") or 0) > 0]
        ttft_vals = [r.get("mean_ttft_ms", 0) or 0 for r in self._full_rows if (r.get("mean_ttft_ms") or 0) > 0]
        passes    = sum(1 for r in self._full_rows if r.get("tier_pass") == "PASS")
        n = len(self._full_rows)
        parts = [f"[dim]{n} run{'s' if n != 1 else ''}[/dim]"]
        if tps_vals:
            parts.append(f"best TPS [bold]{max(tps_vals):.1f}[/bold]")
        if ttft_vals:
            parts.append(f"best TTFT [bold]{min(ttft_vals):.0f} ms[/bold]")
        parts.append(f"pass rate [bold]{passes}/{n}[/bold]")
        stats_lbl.update("  ·  ".join(parts))
