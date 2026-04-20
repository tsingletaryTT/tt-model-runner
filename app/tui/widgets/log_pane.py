#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""LogPane — Logs tab for the Textual TUI."""
from __future__ import annotations

import re
from typing import List, Set, Tuple

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Label, ProgressBar, RichLog, Static

_LEVEL_RE = re.compile(r'\b(ERROR|CRITICAL|WARN|WARNING|INFO|DEBUG)\b')

_ALL_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


def _detect_level(line: str) -> str:
    """Return normalised log level string, or '' if none detected."""
    m = _LEVEL_RE.search(line)
    if not m:
        return ""
    lvl = m.group(1)
    if lvl == "CRITICAL":
        return "ERROR"
    if lvl == "WARNING":
        return "WARN"
    return lvl


class LogPane(Widget):
    """Logs tab: live server output with loading status widgets."""

    DEFAULT_CSS = """
    LogPane {
        height: 100%;
        layout: vertical;
    }
    #log-stepper {
        color: $text-muted;
        padding: 0 1;
    }
    #log-progress {
        margin: 0 1;
    }
    #log-progress-label {
        color: $text-muted;
        padding: 0 1;
    }
    #tour-panel {
        height: 6;
        layout: horizontal;
        border: solid $primary-darken-2;
        display: none;
        margin: 0 1;
    }
    #tour-left {
        width: 1fr;
        padding: 0 1;
        color: $text-muted;
    }
    #tour-right {
        width: 1fr;
        padding: 0 1;
    }
    #log-filter-bar {
        height: 1;
        layout: horizontal;
        padding: 0 1;
        color: $text-muted;
    }
    #log-output {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("d", "toggle_debug", "Debug", show=False),
        Binding("i", "toggle_info",  "Info",  show=False),
        Binding("w", "toggle_warn",  "Warn",  show=False),
        Binding("e", "toggle_error", "Error", show=False),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._all_lines: List[Tuple[str, str]] = []   # (text, level) pairs
        self._hidden_levels: Set[str] = set()         # levels to suppress

    def compose(self) -> ComposeResult:
        yield Static("", id="log-banner")
        yield Static("", id="log-stepper")
        yield ProgressBar(total=100, show_eta=False, id="log-progress")
        yield Static("", id="log-progress-label")
        with Widget(id="tour-panel"):
            yield Static("", id="tour-left")
            yield Static("", id="tour-right")
        yield Static(self._filter_markup(), id="log-filter-bar", markup=True)
        yield RichLog(id="log-output", highlight=False, markup=False, wrap=True)

    def update_state(self, state, info: str = "") -> None:
        name = state.name if hasattr(state, "name") else str(state)
        self.query_one("#log-banner", Static).update(f"{name}  {info}".strip())
        loading = name in ("LOADING", "LAUNCHING", "PULLING_IMAGE")
        self.query_one("#log-progress").display = loading
        self.query_one("#log-progress-label").display = loading
        self.query_one("#tour-panel").display = loading

    def update_progress(self, fraction: float, label: str) -> None:
        bar = self.query_one("#log-progress", ProgressBar)
        if fraction < 0:
            bar.advance(1)
        else:
            bar.progress = int(fraction * 100)
        self.query_one("#log-progress-label", Static).update(label)

    def update_substage(self, stepper: str, left: str, right: str, dots: str) -> None:
        self.query_one("#log-stepper", Static).update(stepper)
        self.query_one("#tour-left", Static).update(left)
        self.query_one("#tour-right", Static).update(right)
        self.query_one("#tour-panel").display = bool(stepper)

    def append_line(self, line: str) -> None:
        level = _detect_level(line)
        self._all_lines.append((line, level))
        if level not in self._hidden_levels:
            self.query_one("#log-output", RichLog).write(line)

    # ── Filter actions ───────────────────────────────────────────────────────

    def action_toggle_debug(self) -> None: self._toggle_level("DEBUG")
    def action_toggle_info(self)  -> None: self._toggle_level("INFO")
    def action_toggle_warn(self)  -> None: self._toggle_level("WARN")
    def action_toggle_error(self) -> None: self._toggle_level("ERROR")

    def _toggle_level(self, level: str) -> None:
        if level in self._hidden_levels:
            self._hidden_levels.discard(level)
        else:
            self._hidden_levels.add(level)
        self._rebuild_log()
        self._update_filter_bar()

    def _rebuild_log(self) -> None:
        log = self.query_one("#log-output", RichLog)
        log.clear()
        for line, level in self._all_lines:
            if level not in self._hidden_levels:
                log.write(line)

    def _update_filter_bar(self) -> None:
        self.query_one("#log-filter-bar", Static).update(
            self._filter_markup(), markup=True
        )

    def _filter_markup(self) -> str:
        parts = []
        labels = {"DEBUG": "D", "INFO": "I", "WARN": "W", "ERROR": "E"}
        for lvl, short in labels.items():
            if lvl in self._hidden_levels:
                parts.append(f"[dim]{short}[/dim]")
            else:
                parts.append(f"[bold]{short}[/bold]")
        return "[dim]Filter:[/dim] " + " ".join(parts) + "  [dim][D/I/W/E toggle][/dim]"
