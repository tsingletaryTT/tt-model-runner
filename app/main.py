#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Entry point for tt-model-runner-gui."""
import logging
import logging.handlers
import sys
from pathlib import Path

_LOG_PATH = Path.home() / ".config" / "tt-runner-gui" / "app.log"
_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            _LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        ),
    ],
)
# Also surface WARNING+ to stderr so terminal users see issues
logging.getLogger().addHandler(logging.StreamHandler(sys.stderr))
logging.getLogger("urllib3").setLevel(logging.WARNING)

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, Gio, GLib

_CSS = b"""
@define-color tt_bg_panel    #0A1F28;
@define-color tt_bg_darkest  #0F2A35;
@define-color tt_bg_dark     #1A3C47;
@define-color tt_border      #2D5566;
@define-color tt_accent      #4FD1C5;
@define-color tt_accent_light #81E6D9;
@define-color tt_text        #E8F0F2;
@define-color tt_text_muted  #607D8B;
@define-color tt_pink        #EC96B8;
@define-color tt_success     #27AE60;
@define-color tt_error       #FF6B6B;
@define-color tt_warning     #F4C471;

window, .view { background-color: @tt_bg_darkest; color: @tt_text; }
* { font-family: "Noto Sans", "Segoe UI", sans-serif; font-size: 13px; color: @tt_text; }
.section-label { color: @tt_accent; font-weight: bold; font-size: 11px; }
.muted { color: @tt_text_muted; font-size: 11px; }

entry, textview {
    background-color: @tt_bg_dark; color: @tt_text;
    border: 1px solid @tt_border; border-radius: 4px; padding: 4px;
}
entry:focus { border-color: @tt_accent; }

button {
    background-color: @tt_bg_dark; color: @tt_text;
    border: 1px solid @tt_border; border-radius: 4px; padding: 5px 10px;
}
button:hover { background-color: @tt_border; border-color: @tt_accent; }
button:disabled { color: @tt_text_muted; border-color: @tt_bg_dark; }

.launch-btn {
    background-color: @tt_accent; color: @tt_bg_darkest;
    font-weight: bold; border: none; padding: 8px 14px;
}
.launch-btn:hover { background-color: @tt_accent_light; }
.launch-btn:disabled { background-color: @tt_bg_dark; color: @tt_text_muted; border: 1px solid @tt_border; }

.stop-btn {
    background-color: @tt_error; color: white;
    font-weight: bold; border: none; padding: 8px 14px;
}

.pill { border-radius: 10px; padding: 2px 8px; font-size: 11px; font-weight: bold; }
.pill-idle    { background-color: @tt_bg_dark; color: @tt_text_muted; }
.pill-loading { background-color: @tt_accent; color: @tt_bg_darkest; }
.pill-ready   { background-color: @tt_success; color: white; }
.pill-error   { background-color: @tt_error; color: white; }
.pill-stopping { background-color: @tt_pink; color: @tt_bg_darkest; }

.hf-ok   { color: @tt_success; font-size: 11px; }
.hf-warn { color: @tt_error; font-size: 11px; }

.tour-panel {
    background-color: @tt_bg_panel;
    border: 1px solid @tt_border;
    border-radius: 4px;
}

.log-view {
    background-color: @tt_bg_panel;
    font-family: "Noto Sans Mono", "DejaVu Sans Mono", monospace;
    font-size: 11px;
}

separator { background-color: @tt_border; min-height: 1px; min-width: 1px; }
treeview { background-color: @tt_bg_panel; }
treeview:selected { background-color: @tt_bg_dark; color: @tt_accent; }

progressbar trough { background-color: @tt_bg_dark; border-radius: 3px; min-height: 6px; }
progressbar progress { background-color: @tt_accent; border-radius: 3px; }
"""


class App(Gtk.Application):
    def __init__(self):
        super().__init__(
            application_id="ai.tenstorrent.tt-model-runner-gui",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )

    def do_activate(self):
        # Bootstrap timing data on first run
        from pathlib import Path
        from timing_store import TimingStore
        timing_path = Path.home() / ".config" / "tt-runner-gui" / "timing.json"
        if not timing_path.exists():
            TimingStore(timing_path)  # writes bootstrap data to disk

        from main_window import MainWindow
        win = MainWindow(application=self)

        provider = Gtk.CssProvider()
        provider.load_from_data(_CSS)
        Gtk.StyleContext.add_provider_for_display(
            win.get_display(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        win.present()


def main():
    app = App()
    sys.exit(app.run(sys.argv))


if __name__ == "__main__":
    main()
