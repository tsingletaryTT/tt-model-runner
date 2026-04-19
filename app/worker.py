# app/worker.py
# SPDX-License-Identifier: Apache-2.0
"""Dispatch shim — backward-compatible wrapper used by health_worker and
server_manager while they are being migrated to AppController.

GTK main.py calls set_dispatch(GLib.idle_add) at startup so that legacy
callers still post to the GTK main thread.  The TUI sets its own dispatch.
New code should use AppController._emit() instead of this module.
"""
from typing import Any, Callable

_dispatch: Callable = lambda fn, *a: fn(*a)


def set_dispatch(fn: Callable) -> None:
    """Set the event-loop dispatch function for this process.

    Call once at startup before any background threads start.
    fn(callback, *args) must schedule callback(*args) on the UI event loop.
    """
    global _dispatch
    _dispatch = fn


def idle_add_once(fn: Callable, *args: Any) -> None:
    """Schedule fn(*args) on the UI event loop via the registered dispatch fn."""
    _dispatch(fn, *args)
