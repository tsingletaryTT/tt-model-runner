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
        height: 10;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Local mirror used to compute aggregate stats without re-reading the controller.
        self._result_rows: list = []  # list of dicts with mean_tps, mean_ttft_ms, tier_pass

    def compose(self) -> ComposeResult:
        with Widget(id="bench-config-row"):
            yield Label("Mode: ")
            yield Select(_MODES, value="smoke-test", id="bench-mode")
            yield Checkbox("Concurrency sweeps", id="bench-sweeps")
            yield Checkbox("Percentile report",  id="bench-pct")
            yield Button("▶ Run Benchmark", id="bench-run-btn", variant="success")
        with Widget(id="bench-hist-row"):
            yield Button("⬇ Export CSV", id="bench-csv-btn", variant="default")
            yield Button("✕ Clear History", id="bench-clear-btn", variant="default")
        yield Label("", id="bench-stats", markup=True)
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
        self._result_rows = []
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
            self._result_rows.append({
                "mean_tps": r.get("mean_tps", 0) or 0,
                "mean_ttft_ms": r.get("mean_ttft_ms", 0) or 0,
                "tier_pass": r.get("tier_pass", ""),
            })
        self._update_stats()

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
        self._result_rows.append({
            "mean_tps": result.mean_tps or 0,
            "mean_ttft_ms": result.mean_ttft_ms or 0,
            "tier_pass": result.tier_pass,
        })
        self._update_stats()

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

    def _update_stats(self) -> None:
        """Recompute and display aggregate stats from all stored results."""
        try:
            stats_lbl = self.query_one("#bench-stats", Label)
        except Exception:
            return
        if not self._result_rows:
            stats_lbl.update("")
            return
        tps_vals  = [r["mean_tps"]     for r in self._result_rows if r["mean_tps"] > 0]
        ttft_vals = [r["mean_ttft_ms"] for r in self._result_rows if r["mean_ttft_ms"] > 0]
        passes    = sum(1 for r in self._result_rows if r["tier_pass"] == "PASS")
        n = len(self._result_rows)
        parts = [f"[dim]{n} run{'s' if n != 1 else ''}[/dim]"]
        if tps_vals:
            parts.append(f"best TPS [bold]{max(tps_vals):.1f}[/bold]")
        if ttft_vals:
            parts.append(f"best TTFT [bold]{min(ttft_vals):.0f} ms[/bold]")
        parts.append(f"pass rate [bold]{passes}/{n}[/bold]")
        stats_lbl.update("  ·  ".join(parts))

    def _do_clear_history(self) -> None:
        app_ctrl = getattr(self.app, "_ctrl", None)
        if not app_ctrl:
            return
        app_ctrl.clear_bench_history()
        self._result_rows = []
        self.query_one("#bench-results", DataTable).clear()
        self._update_stats()
        self.notify("Benchmark history cleared")
