#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""GLib.idle_add threading helpers.

GTK is single-threaded. All widget updates from background threads MUST go
through idle_add. Touching widgets from a thread causes silent data corruption
or hard crashes.
"""
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib
from typing import Callable, Any


def idle_add(fn: Callable, *args: Any) -> None:
    """Schedule fn(*args) to run on the GTK main thread."""
    GLib.idle_add(fn, *args)


def idle_add_once(fn: Callable, *args: Any) -> None:
    """Schedule fn(*args) on GTK main thread; wrapper returns GLib.SOURCE_REMOVE."""
    def wrapper():
        fn(*args)
        return GLib.SOURCE_REMOVE
    GLib.idle_add(wrapper)
