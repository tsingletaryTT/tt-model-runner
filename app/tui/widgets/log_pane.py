#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""LogPane — Logs tab for the Textual TUI."""
from __future__ import annotations

import random
import re
import time
from typing import Dict, List, Optional, Set, Tuple

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Input, Label, ProgressBar, RichLog, Static

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
    """Logs tab: live server output with log level filtering and text search."""

    DEFAULT_CSS = """
    LogPane {
        height: 100%;
        layout: vertical;
    }
    #dyk-panel {
        height: auto;
        display: none;
        border: solid $primary-darken-2;
        padding: 0 1;
        margin: 0 1 0 1;
        color: $text-muted;
    }
    #dyk-nav {
        height: 1;
        layout: horizontal;
        color: $text-muted;
    }
    #dyk-tag {
        width: 12;
    }
    #dyk-counter {
        width: 1fr;
        text-align: right;
    }
    #log-stepper {
        color: $text-muted;
        padding: 0 1;
    }
    #log-progress {
        margin: 0 1;
        display: none;
    }
    #log-progress-label {
        color: $text-muted;
        padding: 0 1;
        display: none;
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
    #log-search-row {
        height: 1;
        layout: horizontal;
        display: none;
    }
    #log-search-input {
        width: 1fr;
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
        Binding("d",       "toggle_debug",  "Debug",  show=False),
        Binding("i",       "toggle_info",   "Info",   show=False),
        Binding("w",       "toggle_warn",   "Warn",   show=False),
        Binding("e",       "toggle_error",  "Error",  show=False),
        Binding("ctrl+f",  "open_search",   "Search", show=False),
        Binding("escape",  "close_search",  "Close",  show=False),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._all_lines: List[Tuple[str, str]] = []   # (text, level) pairs
        self._hidden_levels: Set[str] = set()         # levels to suppress
        self._search_text: str = ""                   # active search filter
        self._ready_start: Optional[float] = None     # monotonic timestamp when READY
        self._uptime_timer = None
        self._banner_base: str = ""
        self._ad_cards: List[Dict] = []               # rotating DYK / model-rec cards
        self._ad_idx: int = 0
        self._ad_timer = None

    def compose(self) -> ComposeResult:
        yield Static("", id="log-banner")
        with Widget(id="dyk-panel"):
            with Widget(id="dyk-nav"):
                yield Static("", id="dyk-tag", markup=True)
                yield Static("", id="dyk-counter", markup=True)
            yield Static("", id="dyk-headline", markup=True)
            yield Static("", id="dyk-body", markup=True)
        yield Static("", id="log-stepper")
        yield ProgressBar(total=100, show_eta=False, id="log-progress")
        yield Static("", id="log-progress-label")
        with Widget(id="tour-panel"):
            yield Static("", id="tour-left")
            yield Static("", id="tour-right")
        with Widget(id="log-search-row"):
            yield Input(placeholder="Search logs… (Esc to close)", id="log-search-input")
        yield Static(self._filter_markup(), id="log-filter-bar", markup=True)
        yield RichLog(id="log-output", highlight=True, markup=False, wrap=True)

    def update_state(self, state, info: str = "") -> None:
        name = state.name if hasattr(state, "name") else str(state)
        self._banner_base = f"{name}  {info}".strip()
        self.query_one("#log-banner", Static).update(self._banner_base)
        loading = name in ("LOADING", "LAUNCHING", "PULLING_IMAGE")
        self.query_one("#log-progress").display = loading
        self.query_one("#log-progress-label").display = loading
        self.query_one("#tour-panel").display = loading

        idle = name in ("IDLE", "DONE", "ERROR")
        dyk = self.query_one("#dyk-panel")
        dyk.display = idle and bool(self._ad_cards)
        if idle and self._ad_cards:
            self._start_ad_timer()
        else:
            self._stop_ad_timer()

        if name == "READY":
            if self._ready_start is None:
                self._ready_start = time.monotonic()
                self._start_uptime_ticker()
        else:
            self._ready_start = None
            self._stop_uptime_ticker()

    # ── Did-you-know / model-rec cards ──────────────────────────────────────

    def set_ad_cards(self, cards: list) -> None:
        """Replace the rotating card pool. Called from app.py after catalog loads."""
        self._ad_cards = list(cards) if cards else []
        self._ad_idx = 0
        self._show_ad_card()
        # Reveal panel if we're currently IDLE.
        try:
            panel = self.query_one("#dyk-panel")
            banner = self.query_one("#log-banner", Static).renderable
            idle = any(s in str(banner) for s in ("IDLE", "DONE", "ERROR"))
            panel.display = idle and bool(self._ad_cards)
            if panel.display:
                self._start_ad_timer()
        except Exception:
            pass

    def _show_ad_card(self) -> None:
        if not self._ad_cards:
            return
        card = self._ad_cards[self._ad_idx % len(self._ad_cards)]
        tag = card.get("tag", "Tip")
        headline = card.get("headline", "")
        body = card.get("body", "")
        n = len(self._ad_cards)
        pos = (self._ad_idx % n) + 1
        try:
            _TAG_COLORS = {"Model": "cyan", "Ecosystem": "yellow", "Did you know?": "green"}
            color = _TAG_COLORS.get(tag, "blue")
            self.query_one("#dyk-tag",     Static).update(f"[{color}]{tag}[/{color}]")
            self.query_one("#dyk-counter", Static).update(f"[dim]{pos}/{n}[/dim]")
            self.query_one("#dyk-headline", Static).update(f"[bold]{headline}[/bold]")
            self.query_one("#dyk-body",     Static).update(f"[dim]{body}[/dim]")
        except Exception:
            pass

    def _start_ad_timer(self) -> None:
        if self._ad_timer is not None:
            return
        self._ad_timer = self.set_interval(8, self._rotate_ad_card)

    def _stop_ad_timer(self) -> None:
        if self._ad_timer is not None:
            try:
                self._ad_timer.stop()
            except Exception:
                pass
            self._ad_timer = None

    def _rotate_ad_card(self) -> None:
        if not self._ad_cards:
            return
        self._ad_idx = (self._ad_idx + 1) % len(self._ad_cards)
        self._show_ad_card()

    # ── Uptime ticker ────────────────────────────────────────────────────────

    def _start_uptime_ticker(self) -> None:
        if self._uptime_timer is not None:
            return
        self._uptime_timer = self.set_interval(5, self._tick_uptime)

    def _stop_uptime_ticker(self) -> None:
        if self._uptime_timer is not None:
            try:
                self._uptime_timer.stop()
            except Exception:
                pass
            self._uptime_timer = None

    def _tick_uptime(self) -> None:
        if self._ready_start is None:
            return
        elapsed = int(time.monotonic() - self._ready_start)
        if elapsed < 60:
            uptime = f"↑ {elapsed}s"
        elif elapsed < 3600:
            uptime = f"↑ {elapsed // 60}m{elapsed % 60:02d}s"
        else:
            h = elapsed // 3600
            m = (elapsed % 3600) // 60
            uptime = f"↑ {h}h{m:02d}m"
        base = getattr(self, "_banner_base", "")
        self.query_one("#log-banner", Static).update(f"{base}  {uptime}")

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
        if self._line_visible(line, level):
            self.query_one("#log-output", RichLog).write(line)

    # ── Search ───────────────────────────────────────────────────────────────

    def action_open_search(self) -> None:
        row = self.query_one("#log-search-row")
        row.display = True
        self.query_one("#log-search-input", Input).focus()

    def action_close_search(self) -> None:
        row = self.query_one("#log-search-row")
        if row.display:
            self._search_text = ""
            self.query_one("#log-search-input", Input).value = ""
            row.display = False
            self._rebuild_log()
            self._update_filter_bar()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "log-search-input":
            self._search_text = event.value.lower()
            self._rebuild_log()
            self._update_filter_bar()

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

    def _line_visible(self, line: str, level: str) -> bool:
        if level in self._hidden_levels:
            return False
        if self._search_text and self._search_text not in line.lower():
            return False
        return True

    def _rebuild_log(self) -> None:
        log = self.query_one("#log-output", RichLog)
        log.clear()
        for line, level in self._all_lines:
            if self._line_visible(line, level):
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
        level_part = "[dim]Filter:[/dim] " + " ".join(parts)
        if self._search_text:
            return level_part + f"  [yellow]search: {self._search_text}[/yellow]  [dim][Esc clear][/dim]"
        return level_part + "  [dim][D/I/W/E] [Ctrl+F search][/dim]"
