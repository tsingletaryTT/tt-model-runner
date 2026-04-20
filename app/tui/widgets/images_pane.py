#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""ImagesPane — Docker images tab for the Textual TUI."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Button, DataTable, Label, Static


class ImagesPane(Widget):
    """Docker images tab: list TT images, refresh, prune dangling."""

    DEFAULT_CSS = """
    ImagesPane {
        height: 100%;
        layout: vertical;
        padding: 0 1;
    }
    #images-btn-row {
        height: 3;
        layout: horizontal;
    }
    #images-status {
        color: $text-muted;
        height: 1;
    }
    #images-table {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        with Widget(id="images-btn-row"):
            yield Button("↺ Refresh", id="images-refresh-btn", variant="default")
            yield Button("✕ Prune Dangling", id="images-prune-btn", variant="default")
        yield Static("", id="images-status")
        yield Label("TT Docker Images")
        yield DataTable(id="images-table")

    def on_mount(self) -> None:
        table = self.query_one("#images-table", DataTable)
        table.add_columns("Image", "Size", "Created")

    def load_images(self, images: list) -> None:
        table = self.query_one("#images-table", DataTable)
        table.clear()
        for img in images:
            table.add_row(img.short_tag, img.size_str, img.created_str)
        status = f"{len(images)} TT image(s)" if images else "No TT images found"
        self.query_one("#images-status", Static).update(status)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        app_ctrl = getattr(self.app, "_ctrl", None)
        if not app_ctrl:
            return
        if btn_id == "images-refresh-btn":
            self.query_one("#images-status", Static).update("Scanning…")
            app_ctrl.scan_docker_images_async()
        elif btn_id == "images-prune-btn":
            self.query_one("#images-status", Static).update("Pruning dangling images…")
            app_ctrl.prune_docker_images(
                on_complete=lambda: self.app.call_from_thread(self._on_prune_done)
            )

    def _on_prune_done(self) -> None:
        self.notify("Prune complete", title="Docker prune")
        app_ctrl = getattr(self.app, "_ctrl", None)
        if app_ctrl:
            app_ctrl.scan_docker_images_async()
