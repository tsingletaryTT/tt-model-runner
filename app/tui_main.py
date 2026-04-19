#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""TUI entry point — launch the Textual-based terminal interface."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def main() -> None:
    from tui.app import TuiApp
    app = TuiApp()
    app.run()


if __name__ == "__main__":
    main()
