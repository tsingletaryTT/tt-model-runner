#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""ToolPane — Tools tab for the Textual TUI."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Button, Input, Label, RichLog, TextArea

_SAMPLE_TOOL_JSON = '''\
[
  {
    "type": "function",
    "function": {
      "name": "get_weather",
      "description": "Get current weather for a city",
      "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"]
      }
    }
  }
]'''


class ToolPane(Widget):
    """Tools tab: tool definition editor, prompt, round-trip display."""

    DEFAULT_CSS = """
    ToolPane {
        height: 100%;
        layout: horizontal;
        padding: 0 1;
    }
    #tool-left {
        width: 1fr;
        layout: vertical;
    }
    #tool-right {
        width: 1fr;
        layout: vertical;
    }
    #tool-def {
        height: 1fr;
        border: solid $primary-darken-2;
    }
    #tool-output {
        height: 1fr;
        border: solid $primary-darken-2;
    }
    #tool-hint {
        color: $warning;
        display: none;
    }
    """

    def compose(self) -> ComposeResult:
        with Widget(id="tool-left"):
            yield Label("TOOL DEFINITION (JSON)")
            # Omit language= so Textual falls back to plain text when
            # tree-sitter-json is not installed (avoids LanguageDoesNotExist).
            yield TextArea(_SAMPLE_TOOL_JSON, id="tool-def",
                           theme="monokai")
        with Widget(id="tool-right"):
            yield Label("PROMPT")
            yield Input(placeholder="What's the weather in Austin?",
                        id="tool-prompt")
            yield Button("▶ Send", id="tool-send-btn", variant="success")
            yield Label("⚠ Tool use was not enabled at launch. Re-launch with tool use on.",
                        id="tool-hint")
            yield Label("ROUND-TRIP")
            yield RichLog(id="tool-output", highlight=False, markup=False)

    def append_round_trip(self, rt) -> None:
        """Append one ToolRoundTrip step to the output log."""
        log = self.query_one("#tool-output", RichLog)
        if rt.step == "call":
            log.write(f"→ tool_call: {rt.name}({rt.arguments})")
        elif rt.step == "result":
            log.write(f"← tool result: {rt.content}")
        else:
            log.write(f"← final: {rt.content}")

    def show_hint(self, visible: bool) -> None:
        self.query_one("#tool-hint").display = visible

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "tool-send-btn":
            return
        import json
        try:
            raw = self.query_one("#tool-def", TextArea).text
            tools = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.notify(f"Invalid tool JSON: {exc}", severity="error")
            return
        prompt = self.query_one("#tool-prompt", Input).value.strip()
        if not prompt:
            return
        self.query_one("#tool-output", RichLog).clear()
        app_ctrl = getattr(self.app, "_ctrl", None)
        if app_ctrl is None:
            return
        opts = app_ctrl.get_options()
        if not opts.tool_use_enabled:
            self.show_hint(True)
            return
        self.show_hint(False)
        app_ctrl.send_tool_call(tools, prompt)
