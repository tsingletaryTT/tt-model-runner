# TUI, Tool Calling & Benchmarks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement interactive tool calling and benchmark integration in both the existing GTK GUI and a new Textual TUI, all sharing the same AppController from Plan 1.

**Architecture:** Fill AppController's `send_tool_call()` and `run_benchmark()` stubs with background-thread logic; both emit `on_tool_result` / `on_bench_result` callbacks. GTK MainPanel gains Tools and Bench tabs (shown when READY). A new Textual TUI (`app/tui/`) provides a feature-equivalent terminal interface using `call_from_thread` as its dispatch function.

**Tech Stack:** httpx (tool-call HTTP), respx (httpx test mocking), textual≥0.61 (TUI framework)

---

## File Map

| File | Status | Purpose |
|------|--------|---------|
| `app/controller.py` | Modify | Add `ToolRoundTrip`+`BenchResult` dataclasses; fill `send_tool_call()` + `run_benchmark()` |
| `app/tool_client.py` | Create | Synchronous OpenAI-compatible multi-turn tool-call session |
| `app/benchmark_runner.py` | Create | Wraps `run.py --workflow benchmarks`, parses JSON output, evaluates pass/fail |
| `app/main_window.py` | Modify | Add tab bar + Tools + Bench pages to `MainPanel`; wire new callbacks in `MainWindow` |
| `app/tui/__init__.py` | Create | TUI package marker |
| `app/tui/app.py` | Create | `TuiApp` — Textual Application; creates AppController with `call_from_thread` dispatch |
| `app/tui/widgets/__init__.py` | Create | Widgets sub-package marker |
| `app/tui/widgets/model_rail.py` | Create | Collapsible left rail: model/device/port/launch/state |
| `app/tui/widgets/log_pane.py` | Create | Logs tab: state banner, stepper, progress bar, tour panel, scrollable log |
| `app/tui/widgets/config_pane.py` | Create | Config tab: use-case chips, quick settings, command preview |
| `app/tui/widgets/tool_pane.py` | Create | Tools tab: tool JSON editor, prompt input, round-trip output |
| `app/tui/widgets/bench_pane.py` | Create | Bench tab: run config, live log, results table |
| `app/tui_main.py` | Modify | Replace `NotImplementedError` stub with real `TuiApp` launch |
| `tests/test_tool_client.py` | Create | `respx`-mocked tests for tool-call session |
| `tests/test_benchmark_runner.py` | Create | Metric parsing + tier evaluation + persistence tests |

---

### Task 1: Add BenchResult and ToolRoundTrip dataclasses to controller.py

**Files:**
- Modify: `app/controller.py` (after the imports block, before the constants)

- [ ] **Step 1: Insert dataclasses into controller.py**

Open `app/controller.py`. After the `from timing_store import TimingStore` import line, add the two dataclasses:

```python
@dataclass
class ToolRoundTrip:
    """One step in a multi-turn tool-call exchange emitted via on_tool_result."""
    step: str        # "call" | "result" | "final"
    name: str        # tool function name (populated for "call" step)
    arguments: str   # JSON string of arguments (populated for "call" step)
    content: str     # result content or final assistant reply


@dataclass
class BenchResult:
    """Parsed output from one benchmark configuration."""
    model_name: str
    device: str
    timestamp: str
    isl: int
    osl: int
    concurrency: int
    mean_ttft_ms: float
    p95_ttft_ms: Optional[float]
    mean_tps: float
    tps_decode: float
    mean_e2el_ms: float
    request_throughput: float
    tier_pass: str   # "PASS" | "BELOW_TARGET" | "FAIL"
```

Also add to the module-level imports:

```python
from tool_client import run_session as _tc_run_session
```

(This line will fail until Task 2 creates `tool_client.py`. Add it anyway so the import is discoverable for patching, but wrap with a try/except so existing tests don't break while tool_client.py doesn't exist yet:)

```python
try:
    from tool_client import run_session as _tc_run_session
except ImportError:
    _tc_run_session = None  # filled in by Task 2
```

Also store the port when launching — add `self._port = port` inside `launch()` right after the guard check (`if self._state not in ...`):

```python
def launch(self, entry: ModelEntry, port: str,
           options: Optional[LaunchOptions] = None) -> None:
    if self._state not in (ServerState.IDLE, ServerState.ERROR):
        return
    self._port = port          # ← ADD THIS LINE
    self._current_entry = entry
    ...
```

- [ ] **Step 2: Verify existing tests still pass**

```bash
cd /home/ttuser/code/tt-model-runner-gui
python -m pytest tests/ -q
```

Expected: All 15 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add app/controller.py
git commit -m "feat: add ToolRoundTrip and BenchResult dataclasses; store port on launch"
```

---

### Task 2: Implement tool_client.py and its tests

**Files:**
- Create: `app/tool_client.py`
- Create: `tests/test_tool_client.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_tool_client.py`:

```python
# tests/test_tool_client.py
"""Synchronous tool-call session tests — httpx requests mocked with respx."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import json
import pytest
import respx
import httpx

BASE_URL = "http://localhost:8000"

SAMPLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Return current weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]


@respx.mock
def test_single_tool_call_round_trip():
    """model calls tool → auto result injected → model replies → final step."""
    from tool_client import run_session

    respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        side_effect=[
            httpx.Response(200, json={
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call_1",
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"city": "Austin"}',
                            },
                        }],
                    }
                }]
            }),
            httpx.Response(200, json={
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "It's 82°F and sunny in Austin.",
                        "tool_calls": None,
                    }
                }]
            }),
        ]
    )

    steps = list(run_session(BASE_URL, "test-model", SAMPLE_TOOLS,
                              "What's the weather in Austin?"))

    assert len(steps) == 3
    assert steps[0][0] == "tool_call"
    assert steps[0][1].name == "get_weather"
    assert json.loads(steps[0][1].arguments)["city"] == "Austin"

    assert steps[1][0] == "tool_result"
    assert "get_weather" in steps[1][1]   # auto-generated result references the tool name

    assert steps[2][0] == "final"
    assert "Austin" in steps[2][1]


@respx.mock
def test_no_tool_call_passthrough():
    """Direct answer (no tools): yields exactly one final step."""
    from tool_client import run_session

    respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "The capital of France is Paris.",
                    "tool_calls": None,
                }
            }]
        })
    )

    steps = list(run_session(BASE_URL, "test-model", SAMPLE_TOOLS,
                              "What is the capital of France?"))

    assert len(steps) == 1
    assert steps[0][0] == "final"
    assert "Paris" in steps[0][1]


@respx.mock
def test_http_error_raises():
    """HTTP 500 propagates as httpx.HTTPStatusError to the caller."""
    from tool_client import run_session

    respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    with pytest.raises(httpx.HTTPStatusError):
        list(run_session(BASE_URL, "test-model", SAMPLE_TOOLS, "Hello"))


@respx.mock
def test_two_tool_calls_in_one_turn():
    """Multiple tool_calls in one assistant message are each yielded as separate steps."""
    from tool_client import run_session

    respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        side_effect=[
            httpx.Response(200, json={
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": "c1", "function": {"name": "get_weather", "arguments": '{"city":"A"}'}},
                            {"id": "c2", "function": {"name": "get_weather", "arguments": '{"city":"B"}'}},
                        ],
                    }
                }]
            }),
            httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": "Done.", "tool_calls": None}}]
            }),
        ]
    )

    steps = list(run_session(BASE_URL, "m", SAMPLE_TOOLS, "weather A and B?"))
    # call A, result A, call B, result B, final
    assert len(steps) == 5
    assert steps[0] == ("tool_call", steps[0][1])
    assert steps[0][1].name == "get_weather"
    assert steps[2][1].arguments == '{"city":"B"}'
    assert steps[4][0] == "final"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /home/ttuser/code/tt-model-runner-gui
python -m pytest tests/test_tool_client.py -v
```

Expected: `ModuleNotFoundError: No module named 'tool_client'`

- [ ] **Step 3: Implement app/tool_client.py**

Create `app/tool_client.py`:

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Synchronous OpenAI-compatible multi-turn tool-call session.

Designed to run in a background threading.Thread (no asyncio).
Yields (step, payload) tuples; AppController emits on_tool_result for each.
"""
import json
from dataclasses import dataclass
from typing import Iterator

import httpx


@dataclass
class ToolCall:
    """One tool invocation the model requested."""
    id: str
    name: str
    arguments: str  # JSON string


def run_session(
    base_url: str,
    model: str,
    tools: list,
    prompt: str,
) -> Iterator[tuple]:
    """Drive a multi-turn conversation until the model produces a final text reply.

    Yields:
        ("tool_call", ToolCall)  — model requested a tool call
        ("tool_result", str)     — auto-generated placeholder result was injected
        ("final", str)           — assistant's final non-tool reply
    """
    messages = [{"role": "user", "content": prompt}]

    with httpx.Client(timeout=120.0) as client:
        while True:
            resp = client.post(
                f"{base_url}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            msg = data["choices"][0]["message"]

            if msg.get("tool_calls"):
                messages.append(msg)
                for tc in msg["tool_calls"]:
                    call = ToolCall(
                        id=tc.get("id", ""),
                        name=tc["function"]["name"],
                        arguments=tc["function"]["arguments"],
                    )
                    yield ("tool_call", call)

                    # Auto-generate a placeholder result so the round-trip always completes
                    result = json.dumps(
                        {"result": f"<{call.name}: auto-generated demo result>"}
                    )
                    yield ("tool_result", result)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result,
                    })
            else:
                yield ("final", msg.get("content", ""))
                break
```

- [ ] **Step 4: Update controller.py try/except import**

Now that `tool_client.py` exists, change the try/except in `app/controller.py` to a plain import:

```python
from tool_client import run_session as _tc_run_session
```

(Remove the `try/except ImportError` wrapper added in Task 1.)

- [ ] **Step 5: Run all tests**

```bash
cd /home/ttuser/code/tt-model-runner-gui
python -m pytest tests/ -v
```

Expected: 4 new PASS (test_tool_client.py) + 15 existing = 19 total PASS.

- [ ] **Step 6: Commit**

```bash
git add app/tool_client.py tests/test_tool_client.py app/controller.py
git commit -m "feat: add tool_client.py — synchronous httpx multi-turn tool-call session"
```

---

### Task 3: Implement AppController.send_tool_call()

**Files:**
- Modify: `app/controller.py`
- Modify: `tests/test_controller.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_controller.py`:

```python
# ── Tool calls ────────────────────────────────────────────────────────────────

_SAMPLE_TOOLS = [
    {"type": "function", "function": {"name": "ping", "description": "test", "parameters": {}}}
]


def test_send_tool_call_emits_tool_result_callbacks():
    """send_tool_call() runs in a background thread and emits on_tool_result for each step."""
    import time
    from controller import ToolRoundTrip
    from tool_client import ToolCall as _TC
    from unittest.mock import patch

    ctrl, view = make_controller()
    results = []
    ctrl.on_tool_result = lambda r: results.append(r)
    ctrl._port = "8000"
    ctrl._current_entry = MagicMock()
    ctrl._current_entry.hf_model_repo = "test-model"

    def _fake_run(base_url, model, tools, prompt):
        yield ("tool_call", _TC(id="c1", name="ping", arguments="{}"))
        yield ("tool_result", '{"pong": true}')
        yield ("final", "Done!")

    with patch("controller._tc_run_session", _fake_run):
        ctrl.send_tool_call(_SAMPLE_TOOLS, "ping")
        for _ in range(50):           # wait up to 2.5s for background thread
            if len(results) >= 3:
                break
            time.sleep(0.05)

    assert len(results) == 3
    assert results[0].step == "call"
    assert results[0].name == "ping"
    assert results[1].step == "result"
    assert results[1].content == '{"pong": true}'
    assert results[2].step == "final"
    assert results[2].content == "Done!"


def test_send_tool_call_emits_error_on_exception():
    """HTTP errors are caught and emitted as a final step with error message."""
    import time
    from unittest.mock import patch

    ctrl, _ = make_controller()
    results = []
    ctrl.on_tool_result = lambda r: results.append(r)
    ctrl._port = "8000"

    def _raise(*args, **kwargs):
        raise RuntimeError("connection refused")
        yield  # make it a generator

    with patch("controller._tc_run_session", _raise):
        ctrl.send_tool_call(_SAMPLE_TOOLS, "hello")
        for _ in range(50):
            if results:
                break
            time.sleep(0.05)

    assert results and results[0].step == "final"
    assert "connection refused" in results[0].content
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /home/ttuser/code/tt-model-runner-gui
python -m pytest tests/test_controller.py::test_send_tool_call_emits_tool_result_callbacks \
                  tests/test_controller.py::test_send_tool_call_emits_error_on_exception -v
```

Expected: FAILED — `send_tool_call` is still a `pass` stub.

- [ ] **Step 3: Fill send_tool_call() in controller.py**

Replace the `pass` stub at the bottom of `app/controller.py`:

```python
def send_tool_call(self, tools: list, prompt: str) -> None:
    """Send a multi-turn tool-call to the running server.

    Runs in a background thread. Emits on_tool_result for each step:
      step="call"   — model requested a tool call
      step="result" — auto-generated result was injected
      step="final"  — model's final text reply (or error message)
    """
    port = getattr(self, "_port", "8000")
    base_url = f"http://localhost:{port}"
    model = (self._current_entry.hf_model_repo
             if self._current_entry else "default")

    def _run() -> None:
        try:
            for step, payload in _tc_run_session(base_url, model, tools, prompt):
                if step == "tool_call":
                    rt = ToolRoundTrip(
                        step="call",
                        name=payload.name,
                        arguments=payload.arguments,
                        content="",
                    )
                elif step == "tool_result":
                    rt = ToolRoundTrip(step="result", name="", arguments="",
                                       content=payload)
                else:
                    rt = ToolRoundTrip(step="final", name="", arguments="",
                                       content=payload)
                self._emit("on_tool_result", rt)
        except Exception as exc:
            self._emit("on_tool_result",
                       ToolRoundTrip(step="final", name="", arguments="",
                                     content=f"Error: {exc}"))

    threading.Thread(target=_run, daemon=True).start()
```

- [ ] **Step 4: Run all tests**

```bash
cd /home/ttuser/code/tt-model-runner-gui
python -m pytest tests/ -q
```

Expected: 21 total PASS (2 new controller tests).

- [ ] **Step 5: Commit**

```bash
git add app/controller.py tests/test_controller.py
git commit -m "feat: implement AppController.send_tool_call() — background thread, emits on_tool_result"
```

---

### Task 4: Add GTK Tools tab to MainPanel

**Files:**
- Modify: `app/main_window.py`

The GTK MainPanel currently has a `_stack` with pages "welcome", "config", "logs".
We add a tab bar (shown only when READY) and a "tools" page.

- [ ] **Step 1: Add tab bar and tools page to MainPanel.__init__**

In `app/main_window.py`, inside `MainPanel.__init__`, find the lines:

```python
        self.append(self._tour_rev)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Stack — holds welcome / config / logs pages.
```

Insert the tab bar BETWEEN the separator and the stack:

```python
        self.append(self._tour_rev)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # ── Ready-state tab bar ───────────────────────────────────────────────
        # Shown only when state == READY to switch between logs/tools/bench pages.
        self._tab_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._tab_bar.set_margin_start(8); self._tab_bar.set_margin_end(8)
        self._tab_bar.set_margin_top(3);   self._tab_bar.set_margin_bottom(3)
        self._tab_btns: dict = {}
        for tab_id, tab_label in [("logs", "Logs"), ("tools", "Tools"), ("bench", "Bench")]:
            btn = Gtk.ToggleButton(label=tab_label)
            btn.add_css_class("log-filter-btn")
            btn.connect("toggled", self._on_tab_toggled, tab_id)
            self._tab_bar.append(btn)
            self._tab_btns[tab_id] = btn
        self._tab_bar.set_visible(False)
        self.append(self._tab_bar)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Stack — holds welcome / config / logs pages.
```

- [ ] **Step 2: Add the tools page to the stack and the tab helper methods**

After the existing `logs_box` is added to the stack (`self._stack.add_named(logs_box, "logs")`), add:

```python
        # ── Tools page ────────────────────────────────────────────────────────
        tools_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        tools_box.set_margin_start(12); tools_box.set_margin_end(12)
        tools_box.set_margin_top(8);    tools_box.set_margin_bottom(8)

        # Tool-not-enabled hint (shown when tool_use_enabled is False at launch)
        self._tool_hint = Gtk.Label(
            label="Tool use was not enabled at launch.\nRe-launch with 🔧 Tool use toggled on."
        )
        self._tool_hint.add_css_class("muted")
        self._tool_hint.set_justify(Gtk.Justification.CENTER)

        # Two-column layout: left = tool definition, right = prompt + output
        tools_cols = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        tools_cols.set_vexpand(True)

        # Left: tool definition editor
        left_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        left_col.set_hexpand(True)
        tool_def_lbl = Gtk.Label(label="TOOL DEFINITION (JSON)")
        tool_def_lbl.add_css_class("muted")
        tool_def_lbl.set_halign(Gtk.Align.START)
        left_col.append(tool_def_lbl)
        self._tool_def_buf = Gtk.TextBuffer()
        self._tool_def_buf.set_text(
            '[\n  {\n    "type": "function",\n'
            '    "function": {\n'
            '      "name": "get_weather",\n'
            '      "description": "Get current weather for a city",\n'
            '      "parameters": {\n'
            '        "type": "object",\n'
            '        "properties": {"city": {"type": "string"}},\n'
            '        "required": ["city"]\n'
            '      }\n    }\n  }\n]'
        )
        tool_def_view = Gtk.TextView(buffer=self._tool_def_buf)
        tool_def_view.set_monospace(True)
        tool_def_view.add_css_class("log-view")
        tool_def_scroll = Gtk.ScrolledWindow()
        tool_def_scroll.set_vexpand(True)
        tool_def_scroll.set_child(tool_def_view)
        left_col.append(tool_def_scroll)
        tools_cols.append(left_col)

        # Right: prompt + send button + round-trip output
        right_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right_col.set_hexpand(True)
        prompt_lbl = Gtk.Label(label="PROMPT")
        prompt_lbl.add_css_class("muted")
        prompt_lbl.set_halign(Gtk.Align.START)
        right_col.append(prompt_lbl)
        self._tool_prompt_entry = Gtk.Entry()
        self._tool_prompt_entry.set_placeholder_text("What's the weather in Austin?")
        right_col.append(self._tool_prompt_entry)
        self._tool_send_btn = Gtk.Button(label="▶ Send")
        self._tool_send_btn.add_css_class("suggested-action")
        right_col.append(self._tool_send_btn)
        output_lbl = Gtk.Label(label="ROUND-TRIP")
        output_lbl.add_css_class("muted")
        output_lbl.set_halign(Gtk.Align.START)
        right_col.append(output_lbl)
        self._tool_output_buf = Gtk.TextBuffer()
        tool_output_view = Gtk.TextView(buffer=self._tool_output_buf)
        tool_output_view.set_editable(False)
        tool_output_view.set_monospace(True)
        tool_output_view.add_css_class("log-view")
        tool_output_scroll = Gtk.ScrolledWindow()
        tool_output_scroll.set_vexpand(True)
        tool_output_scroll.set_child(tool_output_view)
        right_col.append(tool_output_scroll)
        tools_cols.append(right_col)

        tools_box.append(self._tool_hint)
        tools_box.append(tools_cols)
        self._stack.add_named(tools_box, "tools")
```

- [ ] **Step 3: Add tab methods and `append_tool_result()`**

Add these methods to `MainPanel`:

```python
    def show_tools(self) -> None:
        """Switch to the tools tab page."""
        self._stack.set_visible_child_name("tools")
        self._update_tab_buttons("tools")

    def show_bench(self) -> None:
        """Switch to the bench tab page."""
        self._stack.set_visible_child_name("bench")
        self._update_tab_buttons("bench")

    def show_logs_tab(self) -> None:
        """Switch to logs page via tab (only updates the tab highlight)."""
        self._stack.set_visible_child_name("logs")
        self._update_tab_buttons("logs")

    def _update_tab_buttons(self, active_id: str) -> None:
        for tid, btn in self._tab_btns.items():
            btn.handler_block_by_func(self._on_tab_toggled)
            btn.set_active(tid == active_id)
            btn.handler_unblock_by_func(self._on_tab_toggled)

    def _on_tab_toggled(self, btn: Gtk.ToggleButton, tab_id: str) -> None:
        if not btn.get_active():
            return
        self._stack.set_visible_child_name(tab_id)
        self._update_tab_buttons(tab_id)

    def set_tool_use_hint_visible(self, visible: bool) -> None:
        """Show/hide the 'tool use not enabled' hint in the tools page."""
        self._tool_hint.set_visible(visible)

    def append_tool_result(self, rt) -> None:
        """Append a ToolRoundTrip step to the round-trip output buffer."""
        buf = self._tool_output_buf
        end = buf.get_end_iter()
        if buf.get_char_count() > 0:
            buf.insert(end, "\n")
            end = buf.get_end_iter()
        if rt.step == "call":
            buf.insert(end, f"→ tool_call: {rt.name}({rt.arguments})")
        elif rt.step == "result":
            buf.insert(end, f"← tool result: {rt.content}")
        else:
            buf.insert(end, f"→ final: {rt.content}")
```

- [ ] **Step 4: Update `set_state()` to show/hide the tab bar**

In `MainPanel.set_state()`, find the `if state == ServerState.READY:` block and add tab-bar visibility:

```python
    def set_state(self, state: ServerState, info: str = ""):
        label, css_class = _STATE_LABELS.get(state, ("?", "pill-idle"))
        self._pill.set_css_classes(["pill", css_class])
        self._pill.set_text(label)
        if info:
            self._banner_info.set_text(info)

        loading = state in (ServerState.LOADING, ServerState.LAUNCHING, ServerState.PULLING_IMAGE)
        active  = state not in (ServerState.IDLE, ServerState.ERROR)

        self._stepper_rev.set_reveal_child(loading)
        self._progress_rev.set_reveal_child(active and state != ServerState.READY)
        self._tour_rev.set_reveal_child(loading)

        # Show tab bar only when READY; default to Logs tab
        ready = (state == ServerState.READY)
        self._tab_bar.set_visible(ready)
        if ready:
            self._update_tab_buttons("logs")

        if state in (ServerState.LAUNCHING, ServerState.PULLING_IMAGE):
            if not self._pulse_source:
                self._pulse_source = GLib.timeout_add(120, self._pulse_tick)
        else:
            if self._pulse_source:
                GLib.source_remove(self._pulse_source)
                self._pulse_source = None

        if state == ServerState.READY:
            self._progress_bar.set_fraction(1.0)
            GLib.timeout_add(2000, lambda: self._progress_rev.set_reveal_child(False) or False)
```

- [ ] **Step 5: Wire the Send button and on_tool_result in MainWindow**

In `MainWindow.__init__`, find:

```python
        controller.on_tool_result    = lambda r: None
```

Replace with:

```python
        controller.on_tool_result    = self._on_tool_result
```

Add the handler and `_on_launch_clicked_tool_send` method to `MainWindow`:

```python
    def _on_tool_result(self, rt) -> None:
        """Append a ToolRoundTrip step to the tools output buffer."""
        self._panel.append_tool_result(rt)

    def _wire_tool_send(self) -> None:
        """Connect the Send button in the Tools page to send_tool_call().

        Called once when the panel is first shown in READY state.
        """
        if getattr(self, "_tool_send_wired", False):
            return
        self._tool_send_wired = True

        def _on_send(_btn):
            import json
            try:
                tools = json.loads(
                    self._panel._tool_def_buf.get_text(
                        self._panel._tool_def_buf.get_start_iter(),
                        self._panel._tool_def_buf.get_end_iter(),
                        False,
                    )
                )
            except json.JSONDecodeError as e:
                self._panel.append_log(f"⚠ Invalid tool JSON: {e}")
                return
            prompt = self._panel._tool_prompt_entry.get_text().strip()
            if not prompt:
                return
            self._panel._tool_output_buf.set_text("")
            # Check tool_use_enabled
            opts = self._ctrl.get_options()
            self._panel.set_tool_use_hint_visible(not opts.tool_use_enabled)
            if opts.tool_use_enabled:
                self._ctrl.send_tool_call(tools, prompt)

        self._panel._tool_send_btn.connect("clicked", _on_send)
```

Also update `_on_state_changed` to call `_wire_tool_send()` when READY:

```python
    def _on_state_changed(self, state: ServerState, info: str) -> None:
        self._panel.set_state(state, info)
        self._sidebar.set_locked(state not in (ServerState.IDLE, ServerState.ERROR))

        if state in (ServerState.IDLE, ServerState.ERROR):
            entry = self._ctrl.current_entry
            if entry:
                self._panel.show_config(entry, self._on_options_changed)
            else:
                self._panel.show_welcome()
        elif state == ServerState.LAUNCHING:
            self._panel.show_logs()
        elif state == ServerState.READY:
            self._wire_tool_send()    # ← ADD THIS LINE
```

- [ ] **Step 6: Run all tests**

```bash
cd /home/ttuser/code/tt-model-runner-gui
python -m pytest tests/ -q
```

Expected: 21 PASS (GTK tests are not run since no display — just verifying no import errors).

- [ ] **Step 7: Commit**

```bash
git add app/main_window.py
git commit -m "feat: add Tools tab to GTK MainPanel — JSON editor, prompt, round-trip output"
```

---

### Task 5: Implement benchmark_runner.py and its tests

**Files:**
- Create: `app/benchmark_runner.py`
- Create: `tests/test_benchmark_runner.py`
- Create test fixture: `tests/fixtures/benchmark_isl-128_osl-128_maxcon-1_n-100.json`

- [ ] **Step 1: Create test fixtures directory and fixture file**

```bash
mkdir -p /home/ttuser/code/tt-model-runner-gui/tests/fixtures
```

Create `tests/fixtures/benchmark_isl-128_osl-128_maxcon-1_n-100.json`:

```json
{
  "mean_ttft_ms": 145.2,
  "p95_ttft_ms": 312.0,
  "mean_tps": 38.4,
  "tps_decode_throughput": 42.1,
  "mean_e2el_ms": 1820.5,
  "request_throughput": 0.54,
  "num_requests": 100
}
```

- [ ] **Step 2: Write failing tests**

Create `tests/test_benchmark_runner.py`:

```python
# tests/test_benchmark_runner.py
"""BenchmarkRunner unit tests — no subprocess, uses fixture JSON files."""
import sys, os, json, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

FIXTURES = Path(__file__).parent / "fixtures"
BENCH_FILE = FIXTURES / "benchmark_isl-128_osl-128_maxcon-1_n-100.json"


# ── Filename parsing ──────────────────────────────────────────────────────────

def test_parse_filename_extracts_dims():
    from benchmark_runner import _parse_filename
    d = _parse_filename("benchmark_llama_isl-128_osl-256_maxcon-4_n-50.json")
    assert d == {"isl": 128, "osl": 256, "concurrency": 4}


def test_parse_filename_returns_none_on_mismatch():
    from benchmark_runner import _parse_filename
    assert _parse_filename("not_a_benchmark.json") is None


# ── Metric extraction ─────────────────────────────────────────────────────────

def test_parse_json_file_reads_metrics():
    from benchmark_runner import _parse_json_file
    data = _parse_json_file(BENCH_FILE)
    assert data is not None
    assert abs(data["mean_ttft_ms"] - 145.2) < 0.01
    assert abs(data["tps_decode_throughput"] - 42.1) < 0.01


def test_parse_json_file_returns_none_for_missing():
    from benchmark_runner import _parse_json_file
    assert _parse_json_file(Path("/nonexistent/file.json")) is None


# ── Pass/fail evaluation ──────────────────────────────────────────────────────

_TARGETS = {
    "customer_functional": {"mean_tps": 35.0},   # 10% tolerance → need ≥ 31.5
    "functional":          {"mean_tps": 20.0},   # 50% tolerance → need ≥ 10.0
}


def test_eval_tier_pass_above_target():
    from benchmark_runner import _eval_tier
    # mean_tps=38.4 exceeds customer_functional=35.0 (within 10%) → PASS
    assert _eval_tier({"mean_tps": 38.4}, _TARGETS) == "PASS"


def test_eval_tier_below_target_still_functional():
    from benchmark_runner import _eval_tier
    # mean_tps=30.0 < 35.0 but > 20.0*0.5 → BELOW_TARGET
    assert _eval_tier({"mean_tps": 30.0}, _TARGETS) == "BELOW_TARGET"


def test_eval_tier_fail():
    from benchmark_runner import _eval_tier
    # mean_tps=5.0 < 20.0*0.5=10.0 → FAIL
    assert _eval_tier({"mean_tps": 5.0}, _TARGETS) == "FAIL"


def test_eval_tier_no_targets_passes():
    from benchmark_runner import _eval_tier
    assert _eval_tier({"mean_tps": 1.0}, {}) == "PASS"


def test_eval_tier_latency_direction():
    from benchmark_runner import _eval_tier
    # higher latency = worse; customer_functional ttft target 200ms, 10% tol → actual must be ≤ 220ms
    targets = {"customer_functional": {"mean_ttft_ms": 200.0}}
    assert _eval_tier({"mean_ttft_ms": 150.0}, targets) == "PASS"
    assert _eval_tier({"mean_ttft_ms": 250.0}, targets) == "FAIL"


# ── History persistence ───────────────────────────────────────────────────────

def test_persist_appends_to_history(tmp_path):
    """Results are appended to benchmarks.json; subsequent calls accumulate."""
    from benchmark_runner import BenchmarkRunner
    from controller import BenchResult

    results = []
    runner = BenchmarkRunner(
        repo_path=tmp_path,
        on_progress=lambda _: None,
        on_result=lambda r: results.append(r),
    )

    result = BenchResult(
        model_name="Llama-3.1-8B", device="N150", timestamp="2026-04-19T12:00:00",
        isl=128, osl=128, concurrency=1,
        mean_ttft_ms=145.2, p95_ttft_ms=312.0,
        mean_tps=38.4, tps_decode=42.1,
        mean_e2el_ms=1820.5, request_throughput=0.54,
        tier_pass="PASS",
    )
    history_path = tmp_path / "benchmarks.json"
    runner._history_path = history_path
    runner._persist(result)

    data = json.loads(history_path.read_text())
    assert len(data) == 1
    assert data[0]["model_name"] == "Llama-3.1-8B"
    assert data[0]["tier_pass"] == "PASS"

    # Second persist appends
    runner._persist(result)
    data = json.loads(history_path.read_text())
    assert len(data) == 2


# ── Full run (mocked subprocess) ──────────────────────────────────────────────

def test_run_discovers_new_json_files(tmp_path):
    """BenchmarkRunner.run() finds new JSON result files after subprocess exits."""
    from benchmark_runner import BenchmarkRunner
    import shutil

    results = []
    progress = []
    runner = BenchmarkRunner(
        repo_path=tmp_path,
        on_progress=lambda l: progress.append(l),
        on_result=lambda r: results.append(r),
    )
    runner._history_path = tmp_path / "benchmarks.json"

    # Create a fake run.py so the command is valid
    (tmp_path / "run.py").write_text("import sys; sys.exit(0)\n")
    # Create workflow_logs directory with a benchmark result
    logs_dir = tmp_path / "workflow_logs"
    logs_dir.mkdir()
    bench_file = logs_dir / "benchmark_test_isl-128_osl-128_maxcon-1_n-10.json"
    shutil.copy(BENCH_FILE, bench_file)

    # Run synchronously (no thread) by calling _run directly
    runner._run(
        model_name="test-model",
        device="N150",
        mode="smoke-test",
        concurrency_sweeps=False,
        percentile_report=False,
        perf_targets={},
        pre_existing=set(),   # nothing pre-existing → the bench file is "new"
    )

    assert len(results) == 1
    assert results[0].isl == 128
    assert results[0].osl == 128
    assert results[0].concurrency == 1
    assert abs(results[0].mean_ttft_ms - 145.2) < 0.01
```

- [ ] **Step 3: Run tests to confirm they fail**

```bash
cd /home/ttuser/code/tt-model-runner-gui
python -m pytest tests/test_benchmark_runner.py -v
```

Expected: `ModuleNotFoundError: No module named 'benchmark_runner'`

- [ ] **Step 4: Implement app/benchmark_runner.py**

Create `app/benchmark_runner.py`:

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Wraps tt-inference-server run.py --workflow benchmarks.

Runs in a background thread. Streams stdout to on_progress callback.
After subprocess exits, discovers new benchmark_*.json files in workflow_logs/,
parses metrics, evaluates pass/fail against model_spec.json perf_reference
targets, persists results, and calls on_result(BenchResult) for each.
"""
import json
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Set

from controller import BenchResult


_METRIC_MAP = {
    # (output_field, source_json_key)
    "mean_ttft_ms":       "mean_ttft_ms",
    "p95_ttft_ms":        "p95_ttft_ms",
    "mean_tps":           "mean_tps",
    "tps_decode":         "tps_decode_throughput",
    "mean_e2el_ms":       "mean_e2el_ms",
    "request_throughput": "request_throughput",
}


def _parse_filename(name: str) -> Optional[Dict]:
    """Extract isl/osl/concurrency from 'benchmark_*_isl-N_osl-N_maxcon-N*.json'."""
    m = re.search(r"isl-(\d+)_osl-(\d+)_maxcon-(\d+)", name)
    if not m:
        return None
    return {
        "isl": int(m.group(1)),
        "osl": int(m.group(2)),
        "concurrency": int(m.group(3)),
    }


def _parse_json_file(path: Path) -> Optional[Dict]:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _eval_tier(metrics: Dict, targets: Dict) -> str:
    """Evaluate metrics against perf_reference targets.  Returns PASS/BELOW_TARGET/FAIL."""
    if not targets:
        return "PASS"

    def _ok(tier_key: str, tolerance: float) -> bool:
        tier = targets.get(tier_key, {})
        for key, ref in tier.items():
            if not isinstance(ref, (int, float)):
                continue
            actual = metrics.get(key)
            if actual is None:
                continue
            # Throughput/tps: higher is better
            if "tps" in key or "throughput" in key:
                if actual < ref * (1.0 - tolerance):
                    return False
            else:
                # Latency: lower is better
                if actual > ref * (1.0 + tolerance):
                    return False
        return True

    if _ok("customer_functional", 0.10):
        return "PASS"
    if _ok("functional", 0.50):
        return "BELOW_TARGET"
    return "FAIL"


class BenchmarkRunner:
    """Runs tt-inference-server benchmarks and emits results."""

    def __init__(
        self,
        repo_path: Path,
        on_progress: Callable[[str], None],
        on_result: Callable,
    ) -> None:
        self._repo = Path(repo_path)
        self._on_progress = on_progress
        self._on_result = on_result
        self._history_path = (
            Path.home() / ".config" / "tt-runner-gui" / "benchmarks.json"
        )

    def run(
        self,
        model_name: str,
        device: str,
        mode: str = "smoke-test",
        concurrency_sweeps: bool = False,
        percentile_report: bool = False,
        perf_targets: Optional[Dict] = None,
    ) -> None:
        """Start the benchmark in a daemon background thread."""
        logs_dir = self._repo / "workflow_logs"
        pre_existing: Set[Path] = set(logs_dir.glob("benchmark_*.json")) if logs_dir.exists() else set()
        threading.Thread(
            target=self._run,
            args=(model_name, device, mode, concurrency_sweeps,
                  percentile_report, perf_targets or {}, pre_existing),
            daemon=True,
        ).start()

    def _run(
        self,
        model_name: str,
        device: str,
        mode: str,
        concurrency_sweeps: bool,
        percentile_report: bool,
        perf_targets: Dict,
        pre_existing: Set[Path],
    ) -> None:
        cmd = [
            "python3", str(self._repo / "run.py"),
            "--workflow", "benchmarks",
            "--model", model_name,
            "--tt-device", device.lower(),
            "--limit-samples-mode", mode,
        ]
        if concurrency_sweeps:
            cmd.append("--concurrency-sweeps")
        if percentile_report:
            cmd.append("--percentile-report")

        self._on_progress(f"$ {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=self._repo,
            )
            for line in proc.stdout:
                self._on_progress(line.rstrip())
            proc.wait()
        except Exception as exc:
            self._on_progress(f"Error launching benchmark: {exc}")
            return

        logs_dir = self._repo / "workflow_logs"
        new_files = (set(logs_dir.glob("benchmark_*.json")) - pre_existing
                     if logs_dir.exists() else set())
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")

        for path in sorted(new_files):
            dim = _parse_filename(path.name)
            if dim is None:
                continue
            raw = _parse_json_file(path)
            if raw is None:
                continue

            metrics: Dict = {}
            for dest_key, src_key in _METRIC_MAP.items():
                v = raw.get(src_key)
                if v is not None:
                    try:
                        metrics[dest_key] = float(v)
                    except (TypeError, ValueError):
                        pass

            tier = _eval_tier(metrics, perf_targets)
            result = BenchResult(
                model_name=model_name,
                device=device,
                timestamp=timestamp,
                isl=dim["isl"],
                osl=dim["osl"],
                concurrency=dim["concurrency"],
                mean_ttft_ms=metrics.get("mean_ttft_ms", 0.0),
                p95_ttft_ms=metrics.get("p95_ttft_ms"),
                mean_tps=metrics.get("mean_tps", 0.0),
                tps_decode=metrics.get("tps_decode", 0.0),
                mean_e2el_ms=metrics.get("mean_e2el_ms", 0.0),
                request_throughput=metrics.get("request_throughput", 0.0),
                tier_pass=tier,
            )
            self._persist(result)
            self._on_result(result)

    def _persist(self, result: BenchResult) -> None:
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        history = []
        if self._history_path.exists():
            try:
                history = json.loads(self._history_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        history.append({
            "model_name":          result.model_name,
            "device":              result.device,
            "timestamp":           result.timestamp,
            "isl":                 result.isl,
            "osl":                 result.osl,
            "concurrency":         result.concurrency,
            "mean_ttft_ms":        result.mean_ttft_ms,
            "p95_ttft_ms":         result.p95_ttft_ms,
            "mean_tps":            result.mean_tps,
            "tps_decode":          result.tps_decode,
            "mean_e2el_ms":        result.mean_e2el_ms,
            "request_throughput":  result.request_throughput,
            "tier_pass":           result.tier_pass,
        })
        self._history_path.write_text(json.dumps(history, indent=2))
```

- [ ] **Step 5: Run tests**

```bash
cd /home/ttuser/code/tt-model-runner-gui
python -m pytest tests/test_benchmark_runner.py -v
```

Expected: All 10 PASS.

Note: `test_run_discovers_new_json_files` calls `runner._run(...)` directly with `pre_existing=set()`. The signature of `_run()` must accept `pre_existing` as the last positional arg.

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: 31 total PASS.

- [ ] **Step 7: Commit**

```bash
git add app/benchmark_runner.py tests/test_benchmark_runner.py tests/fixtures/
git commit -m "feat: add benchmark_runner.py — wraps run.py benchmarks, parses metrics, evaluates pass/fail"
```

---

### Task 6: Implement AppController.run_benchmark()

**Files:**
- Modify: `app/controller.py`
- Modify: `tests/test_controller.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_controller.py`:

```python
# ── Benchmarks ───────────────────────────────────────────────────────────────

def test_run_benchmark_emits_bench_progress_and_result():
    """run_benchmark() must emit on_bench_progress lines and on_bench_result."""
    import time
    from controller import BenchResult
    from unittest.mock import patch, MagicMock

    ctrl, view = make_controller()
    progress_lines = []
    results = []
    ctrl.on_bench_progress = lambda l: progress_lines.append(l)
    ctrl.on_bench_result   = lambda r: results.append(r)
    ctrl._current_entry = MagicMock()
    ctrl._current_entry.display_name = "test-model"
    ctrl._current_entry.device_type  = "N150"

    fake_result = BenchResult(
        model_name="test-model", device="N150",
        timestamp="2026-01-01T00:00:00",
        isl=128, osl=128, concurrency=1,
        mean_ttft_ms=100.0, p95_ttft_ms=None,
        mean_tps=30.0, tps_decode=32.0,
        mean_e2el_ms=1000.0, request_throughput=0.5,
        tier_pass="PASS",
    )

    class FakeRunner:
        def __init__(self, repo_path, on_progress, on_result):
            on_progress("Running…")
            on_result(fake_result)
        def run(self, *args, **kwargs):
            pass

    with patch("benchmark_runner.BenchmarkRunner", FakeRunner):
        ctrl.run_benchmark(mode="smoke-test")
        for _ in range(50):
            if progress_lines and results:
                break
            time.sleep(0.05)

    assert "Running…" in progress_lines
    assert len(results) == 1
    assert results[0].tier_pass == "PASS"
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
cd /home/ttuser/code/tt-model-runner-gui
python -m pytest tests/test_controller.py::test_run_benchmark_emits_bench_progress_and_result -v
```

Expected: FAILED — `run_benchmark` is still a stub.

- [ ] **Step 3: Fill run_benchmark() in controller.py**

Replace the `pass` stub for `run_benchmark` in `app/controller.py`:

```python
def run_benchmark(
    self,
    mode: str = "smoke-test",
    concurrency_sweeps: bool = False,
    percentile_report: bool = False,
) -> None:
    """Run tt-inference-server benchmarks for the current model.

    Spawns BenchmarkRunner in a background thread. Emits on_bench_progress
    for each log line and on_bench_result for each parsed result file.
    """
    from benchmark_runner import BenchmarkRunner

    repo_path = Path(_settings.server_repo_path)
    if not (repo_path / "run.py").exists():
        self._emit("on_bench_progress",
                   f"⚠ run.py not found at {repo_path} — cannot run benchmark")
        return

    model_name = (self._current_entry.display_name
                  if self._current_entry else "unknown")
    device = (self._current_entry.device_type
              if self._current_entry else "unknown")

    # Extract perf targets for this model from model_spec.json if available
    perf_targets: dict = {}
    spec_path = repo_path / "model_spec.json"
    if self._current_entry and spec_path.exists():
        try:
            spec_data = json.loads(spec_path.read_text())
            model_key = self._current_entry.model_name
            device_key = self._current_entry.device_type
            impl_data = (spec_data.get("model_specs", {})
                         .get(model_key, {}).get(device_key, {}))
            for engine_map in impl_data.values():
                if isinstance(engine_map, dict):
                    for impl in engine_map.values():
                        if isinstance(impl, dict) and "perf_reference" in impl:
                            refs = impl["perf_reference"]
                            if refs:
                                perf_targets = refs[0].get("targets", {})
                            break
                    break
        except Exception:
            pass  # perf_targets stays empty, tier eval will always PASS

    runner = BenchmarkRunner(
        repo_path=repo_path,
        on_progress=lambda line: self._emit("on_bench_progress", line),
        on_result=lambda r: self._emit("on_bench_result", r),
    )
    runner.run(
        model_name=model_name,
        device=device,
        mode=mode,
        concurrency_sweeps=concurrency_sweeps,
        percentile_report=percentile_report,
        perf_targets=perf_targets,
    )
```

Also add `import json` to the top of controller.py if it isn't already there (check — it probably isn't; we use `json.loads` in run_benchmark).

- [ ] **Step 4: Run all tests**

```bash
cd /home/ttuser/code/tt-model-runner-gui
python -m pytest tests/ -q
```

Expected: 32 total PASS.

- [ ] **Step 5: Commit**

```bash
git add app/controller.py tests/test_controller.py
git commit -m "feat: implement AppController.run_benchmark() — launches BenchmarkRunner, emits progress/result"
```

---

### Task 7: Add GTK Bench tab to MainPanel

**Files:**
- Modify: `app/main_window.py`

- [ ] **Step 1: Add the bench page to the stack**

In `app/main_window.py`, after `self._stack.add_named(tools_box, "tools")` (added in Task 4), add:

```python
        # ── Bench page ────────────────────────────────────────────────────────
        bench_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        bench_box.set_margin_start(12); bench_box.set_margin_end(12)
        bench_box.set_margin_top(8);    bench_box.set_margin_bottom(8)

        # Run config row
        run_cfg_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        mode_lbl = Gtk.Label(label="Mode:")
        mode_lbl.add_css_class("muted")
        run_cfg_box.append(mode_lbl)
        self._bench_mode_combo = Gtk.ComboBoxText()
        for m in ["smoke-test", "ci-nightly", "ci-long"]:
            self._bench_mode_combo.append_text(m)
        self._bench_mode_combo.set_active(0)
        run_cfg_box.append(self._bench_mode_combo)
        self._bench_sweeps_check = Gtk.CheckButton(label="Concurrency sweeps")
        run_cfg_box.append(self._bench_sweeps_check)
        self._bench_pct_check = Gtk.CheckButton(label="Percentile report")
        run_cfg_box.append(self._bench_pct_check)
        self._bench_run_btn = Gtk.Button(label="▶ Run Benchmark")
        self._bench_run_btn.add_css_class("suggested-action")
        run_cfg_box.append(self._bench_run_btn)
        bench_box.append(run_cfg_box)
        bench_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Live log
        bench_log_lbl = Gtk.Label(label="LIVE OUTPUT")
        bench_log_lbl.add_css_class("muted")
        bench_log_lbl.set_halign(Gtk.Align.START)
        bench_box.append(bench_log_lbl)
        self._bench_log_buf = Gtk.TextBuffer()
        bench_log_view = Gtk.TextView(buffer=self._bench_log_buf)
        bench_log_view.set_editable(False)
        bench_log_view.set_monospace(True)
        bench_log_view.add_css_class("log-view")
        bench_log_scroll = Gtk.ScrolledWindow()
        bench_log_scroll.set_vexpand(True)
        bench_log_scroll.set_child(bench_log_view)
        bench_box.append(bench_log_scroll)

        # Results section
        bench_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        bench_results_lbl = Gtk.Label(label="RESULTS")
        bench_results_lbl.add_css_class("muted")
        bench_results_lbl.set_halign(Gtk.Align.START)
        bench_box.append(bench_results_lbl)
        self._bench_results_buf = Gtk.TextBuffer()
        bench_results_view = Gtk.TextView(buffer=self._bench_results_buf)
        bench_results_view.set_editable(False)
        bench_results_view.set_monospace(True)
        bench_results_view.add_css_class("log-view")
        bench_results_scroll = Gtk.ScrolledWindow()
        bench_results_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        bench_results_scroll.set_size_request(-1, 120)
        bench_results_scroll.set_child(bench_results_view)
        bench_box.append(bench_results_scroll)

        self._stack.add_named(bench_box, "bench")
```

- [ ] **Step 2: Add append_bench_progress() and append_bench_result() to MainPanel**

```python
    def append_bench_progress(self, line: str) -> None:
        """Append a live log line to the bench live-output buffer."""
        buf = self._bench_log_buf
        end = buf.get_end_iter()
        if buf.get_char_count() > 0:
            buf.insert(end, "\n")
            end = buf.get_end_iter()
        buf.insert(end, line)

    def append_bench_result(self, result) -> None:
        """Append a formatted BenchResult row to the bench results buffer."""
        icon = {"PASS": "✓", "BELOW_TARGET": "⚠", "FAIL": "✗"}.get(result.tier_pass, "?")
        line = (
            f"{icon} {result.tier_pass:12s}"
            f"  ISL={result.isl} OSL={result.osl} Con={result.concurrency}"
            f"  TTFT={result.mean_ttft_ms:.0f}ms"
            f"  TPS={result.mean_tps:.1f}"
            f"  E2EL={result.mean_e2el_ms:.0f}ms"
            f"  [{result.timestamp}]"
        )
        buf = self._bench_results_buf
        end = buf.get_end_iter()
        if buf.get_char_count() > 0:
            buf.insert(end, "\n")
            end = buf.get_end_iter()
        buf.insert(end, line)
```

- [ ] **Step 3: Wire bench callbacks and Run button in MainWindow**

In `MainWindow.__init__`, replace:

```python
        controller.on_bench_progress = self._panel.append_log
        controller.on_bench_result   = lambda r: None
```

With:

```python
        controller.on_bench_progress = self._on_bench_progress
        controller.on_bench_result   = self._on_bench_result
```

Add the handler methods and `_wire_bench_run`:

```python
    def _on_bench_progress(self, line: str) -> None:
        self._panel.append_bench_progress(line)

    def _on_bench_result(self, result) -> None:
        self._panel.append_bench_result(result)

    def _wire_bench_run(self) -> None:
        if getattr(self, "_bench_run_wired", False):
            return
        self._bench_run_wired = True

        def _on_run(_btn):
            mode = self._panel._bench_mode_combo.get_active_text() or "smoke-test"
            sweeps = self._panel._bench_sweeps_check.get_active()
            pct    = self._panel._bench_pct_check.get_active()
            self._panel._bench_log_buf.set_text("")
            self._ctrl.run_benchmark(mode=mode, concurrency_sweeps=sweeps,
                                     percentile_report=pct)

        self._panel._bench_run_btn.connect("clicked", _on_run)
```

Update `_on_state_changed` to also call `_wire_bench_run()` when READY:

```python
        elif state == ServerState.READY:
            self._wire_tool_send()
            self._wire_bench_run()    # ← ADD THIS LINE
```

- [ ] **Step 4: Run all tests**

```bash
cd /home/ttuser/code/tt-model-runner-gui
python -m pytest tests/ -q
```

Expected: 32 PASS (no new tests here — GTK code can't be unit-tested without display).

- [ ] **Step 5: Commit**

```bash
git add app/main_window.py
git commit -m "feat: add Bench tab to GTK MainPanel — mode selector, live log, results display"
```

---

### Task 8: Textual TUI package skeleton + tui_main.py

**Files:**
- Create: `app/tui/__init__.py`
- Create: `app/tui/widgets/__init__.py`
- Create: `app/tui/app.py` (skeleton — imports placeholder widgets)
- Modify: `app/tui_main.py`

- [ ] **Step 1: Create package markers**

Create `app/tui/__init__.py`:
```python
# TUI package — Textual-based terminal interface for tt-model-runner-gui.
```

Create `app/tui/widgets/__init__.py`:
```python
# TUI widget sub-package.
```

- [ ] **Step 2: Create TuiApp skeleton**

Create `app/tui/app.py`:

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""TuiApp — Textual Application for tt-model-runner-gui.

Creates AppController with call_from_thread as dispatch_fn so all on_*
callbacks are safely posted to the Textual event loop.
"""
import sys
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, TabbedContent, TabPane

sys.path.insert(0, str(Path(__file__).parent.parent))

from tui.widgets.model_rail import ModelRail
from tui.widgets.log_pane   import LogPane
from tui.widgets.config_pane import ConfigPane
from tui.widgets.tool_pane  import ToolPane
from tui.widgets.bench_pane import BenchPane


class TuiApp(App[None]):
    """Feature-equivalent TUI sharing AppController with the GTK GUI."""

    CSS = """
    Screen {
        layout: horizontal;
    }
    ModelRail {
        width: 22;
        background: $surface;
        border-right: solid $primary;
    }
    TabbedContent {
        width: 1fr;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("l", "launch_stop", "Launch/Stop"),
        Binding("1", "switch_tab('config')",  "Config",  show=False),
        Binding("2", "switch_tab('logs')",    "Logs",    show=False),
        Binding("3", "switch_tab('tools')",   "Tools",   show=False),
        Binding("4", "switch_tab('bench')",   "Bench",   show=False),
        Binding("[", "toggle_rail",            "Rail",    show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield ModelRail(id="rail")
        with TabbedContent(initial="config", id="tabs"):
            with TabPane("Config", id="config"):
                yield ConfigPane()
            with TabPane("Logs",   id="logs"):
                yield LogPane()
            with TabPane("Tools",  id="tools"):
                yield ToolPane()
            with TabPane("Bench",  id="bench"):
                yield BenchPane()
        yield Footer()

    def on_mount(self) -> None:
        """Create AppController and register all view callbacks."""
        from controller import AppController
        from server_manager import ServerState
        from app_settings import settings as _settings

        self._ctrl = AppController(
            dispatch_fn=lambda fn, *a: self.call_from_thread(fn, *a)
        )

        # ── Register all on_* callbacks ────────────────────────────────────────
        self._ctrl.on_state_changed  = self._on_state_changed
        self._ctrl.on_log_line       = self._on_log_line
        self._ctrl.on_progress       = self._on_progress
        self._ctrl.on_substage       = self._on_substage
        self._ctrl.on_catalog_loaded = self._on_catalog_loaded
        self._ctrl.on_cache_scanned  = lambda _info: None
        self._ctrl.on_bench_progress = self._on_bench_progress
        self._ctrl.on_bench_result   = self._on_bench_result
        self._ctrl.on_tool_result    = self._on_tool_result

        # ── Disable Tools/Bench tabs until READY ──────────────────────────────
        self._set_ready_tabs_enabled(False)

        # ── Wire rail actions ─────────────────────────────────────────────────
        rail = self.query_one(ModelRail)
        rail.on_launch = self._do_launch
        rail.on_stop   = lambda: self._ctrl.stop()

        # ── Auto-discover repo ────────────────────────────────────────────────
        repo_path = None
        saved = _settings.server_repo_path
        if saved:
            p = Path(saved)
            if (p / "run.py").exists() and (p / "model_spec.json").exists():
                repo_path = p
        if not repo_path:
            for candidate in [
                Path.home() / "code" / "tt-inference-server",
                Path.home() / "tt-inference-server",
            ]:
                if (candidate / "run.py").exists():
                    repo_path = candidate
                    break
        if repo_path:
            self.call_after_refresh(lambda: self._ctrl.load_repo(repo_path))

    # ── AppController callbacks ────────────────────────────────────────────────

    def _on_state_changed(self, state, info: str) -> None:
        from server_manager import ServerState
        rail    = self.query_one(ModelRail)
        log_pane = self.query_one(LogPane)
        tabs    = self.query_one(TabbedContent)

        rail.update_state(state, info)
        log_pane.update_state(state, info)

        ready = (state == ServerState.READY)
        self._set_ready_tabs_enabled(ready)
        if state.name in ("LAUNCHING",):
            tabs.active = "logs"
        elif ready:
            tabs.active = "logs"

    def _on_log_line(self, line: str) -> None:
        self.query_one(LogPane).append_line(line)

    def _on_progress(self, fraction: float, label: str) -> None:
        self.query_one(LogPane).update_progress(fraction, label)

    def _on_substage(self, stepper: str, left: str, right: str, dots: str) -> None:
        self.query_one(LogPane).update_substage(stepper, left, right, dots)

    def _on_catalog_loaded(self, catalog, compatible_devices: list) -> None:
        self.query_one(ModelRail).load_catalog(catalog, compatible_devices)

    def _on_bench_progress(self, line: str) -> None:
        self.query_one(BenchPane).append_progress(line)

    def _on_bench_result(self, result) -> None:
        self.query_one(BenchPane).append_result(result)

    def _on_tool_result(self, rt) -> None:
        self.query_one(ToolPane).append_round_trip(rt)

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_launch_stop(self) -> None:
        from server_manager import ServerState
        if self._ctrl.state == ServerState.IDLE or self._ctrl.state.name == "ERROR":
            self._do_launch_from_rail()
        else:
            self._ctrl.stop()

    def _do_launch_from_rail(self) -> None:
        rail = self.query_one(ModelRail)
        entry = rail.selected_entry
        port  = rail.port_value
        if entry:
            opts = self._ctrl.get_options()
            self._ctrl.launch(entry, port, opts)

    def _do_launch(self, entry, port: str) -> None:
        opts = self._ctrl.get_options()
        self._ctrl.launch(entry, port, opts)

    def action_switch_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    def action_toggle_rail(self) -> None:
        rail = self.query_one(ModelRail)
        rail.toggle_class("collapsed")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_ready_tabs_enabled(self, enabled: bool) -> None:
        tabs = self.query_one(TabbedContent)
        for tab_id in ("tools", "bench"):
            tab = tabs.get_tab(tab_id)
            if tab is not None:
                tab.disabled = not enabled
```

- [ ] **Step 3: Update tui_main.py**

Replace `app/tui_main.py` entirely:

```python
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
```

- [ ] **Step 4: Run all tests (only unit tests — no TUI rendering needed)**

```bash
cd /home/ttuser/code/tt-model-runner-gui
python -m pytest tests/ -q
```

Expected: 32 PASS (TUI import errors would only surface at runtime, not in these tests).

- [ ] **Step 5: Commit**

```bash
git add app/tui/__init__.py app/tui/widgets/__init__.py app/tui/app.py app/tui_main.py
git commit -m "feat: add Textual TUI skeleton — TuiApp, package structure, tui_main.py wired"
```

---

### Task 9: ModelRail widget

**Files:**
- Create: `app/tui/widgets/model_rail.py`

The ModelRail is the collapsible left sidebar showing model selection, device, port, launch button, and live state pill.

- [ ] **Step 1: Create app/tui/widgets/model_rail.py**

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""ModelRail — collapsible left sidebar for the Textual TUI.

Shows: model tree (grouped by type/family), device selector, port entry,
launch/stop button, and live state pill.
Calls on_launch(entry, port) and on_stop() callbacks set by TuiApp.
"""
from __future__ import annotations

from typing import Callable, List, Optional, TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button, Label, ListItem, ListView, Select, Static

if TYPE_CHECKING:
    from model_catalog import ModelCatalog, ModelEntry
    from server_manager import ServerState


_STATE_PILLS = {
    "IDLE":          ("● IDLE",     "dim"),
    "LAUNCHING":     ("● LAUNCHING","yellow"),
    "PULLING_IMAGE": ("● PULLING",  "yellow"),
    "LOADING":       ("● LOADING",  "cyan"),
    "READY":         ("● READY",    "green"),
    "ERROR":         ("● ERROR",    "red"),
    "STOPPING":      ("● STOPPING", "yellow"),
}


class ModelRail(Widget):
    """Collapsible left rail. Width: 22 expanded, 4 collapsed."""

    DEFAULT_CSS = """
    ModelRail {
        width: 22;
        height: 100%;
        padding: 0 1;
    }
    ModelRail.collapsed {
        width: 4;
        padding: 0;
    }
    ModelRail > .rail-section-label {
        color: $text-muted;
        text-style: bold;
    }
    """

    # Populated by TuiApp after on_catalog_loaded
    on_launch: Optional[Callable] = None
    on_stop:   Optional[Callable] = None

    selected_entry: Optional["ModelEntry"] = None
    port_value: str = "8000"

    _entries: List["ModelEntry"] = []

    def compose(self) -> ComposeResult:
        yield Static("[b]TT Model Runner[/b]", markup=True)
        yield Static("", id="state-pill")
        yield Label("Model:", classes="rail-section-label")
        yield ListView(id="model-list")
        yield Label("Port:", classes="rail-section-label")
        yield Static("8000", id="port-display")
        yield Button("▶ Launch", id="launch-btn", variant="success")

    def load_catalog(self, catalog: "ModelCatalog", compatible_devices: List[str]) -> None:
        """Populate the model list from the catalog."""
        self._entries = list(catalog.get_compatible(compatible_devices).all_entries()
                             if compatible_devices else catalog.all_entries())
        lv = self.query_one("#model-list", ListView)
        lv.clear()
        for entry in self._entries:
            item = ListItem(Label(f"{entry.display_name}\n  {entry.device_type}"))
            item._entry = entry  # attach entry for on_selected lookup
            lv.append(item)

    def update_state(self, state: "ServerState", info: str) -> None:
        """Update the state pill and toggle the launch/stop button label."""
        pill_text, color = _STATE_PILLS.get(state.name, ("● ?", "dim"))
        self.query_one("#state-pill", Static).update(
            f"[{color}]{pill_text}[/{color}]"
        )
        btn = self.query_one("#launch-btn", Button)
        if state.name in ("IDLE", "ERROR"):
            btn.label = "▶ Launch"
            btn.variant = "success"
        elif state.name == "STOPPING":
            btn.label = "■ Stopping…"
            btn.variant = "warning"
        else:
            btn.label = "■ Stop"
            btn.variant = "error"

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        entry = getattr(item, "_entry", None)
        if entry is not None:
            self.selected_entry = entry

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "launch-btn":
            from server_manager import ServerState
            # Determine current state from parent app
            app_ctrl = getattr(self.app, "_ctrl", None)
            if app_ctrl and app_ctrl.state not in (ServerState.IDLE,):
                if self.on_stop:
                    self.on_stop()
            else:
                if self.selected_entry and self.on_launch:
                    self.on_launch(self.selected_entry, self.port_value)
```

Add `all_entries()` to `ModelCatalog` if it doesn't exist — check `app/model_catalog.py`. If it doesn't have `all_entries()`, add it in a sub-step:

```python
    def all_entries(self) -> List[ModelEntry]:
        return list(self._entries)

    def all_device_types(self) -> List[str]:
        return list({e.device_type for e in self._entries})
```

- [ ] **Step 2: Verify all_entries() already exists in ModelCatalog**

```bash
cd /home/ttuser/code/tt-model-runner-gui
grep -n "def all_entries\|def all_device_types" app/model_catalog.py
```

Expected output shows both methods exist (they were added in Plan 1). No changes needed.

- [ ] **Step 3: Run all tests**

```bash
cd /home/ttuser/code/tt-model-runner-gui
python -m pytest tests/ -q
```

Expected: 32 PASS.

- [ ] **Step 4: Commit**

```bash
git add app/tui/widgets/model_rail.py app/model_catalog.py
git commit -m "feat: add TUI ModelRail widget — collapsible sidebar with model list, launch/stop, state pill"
```

---

### Task 10: LogPane widget

**Files:**
- Create: `app/tui/widgets/log_pane.py`

LogPane mirrors the GTK loading UI: state banner, stepper, progress bar, tour panel, scrollable log with level filter.

- [ ] **Step 1: Create app/tui/widgets/log_pane.py**

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""LogPane — Logs tab for the Textual TUI.

Shows: state banner, stepper text, progress bar, tour panel (two columns),
and a scrollable log view with D/I/W/E level filter key bindings.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Label, ProgressBar, RichLog, Static


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

    def compose(self) -> ComposeResult:
        yield Static("", id="log-banner")
        yield Static("", id="log-stepper")
        yield ProgressBar(total=100, show_eta=False, id="log-progress")
        yield Static("", id="log-progress-label")
        with Widget(id="tour-panel"):
            yield Static("", id="tour-left")
            yield Static("", id="tour-right")
        yield Static("[dim]Filter: [D] [I] [W] [E][/dim]", id="log-filter-bar",
                     markup=True)
        yield RichLog(id="log-output", highlight=False, markup=False, wrap=True)

    def update_state(self, state, info: str = "") -> None:
        """Update the banner text and show/hide loading widgets."""
        name = state.name if hasattr(state, "name") else str(state)
        banner_text = f"{name}  {info}".strip()
        self.query_one("#log-banner", Static).update(banner_text)

        loading = name in ("LOADING", "LAUNCHING", "PULLING_IMAGE")
        self.query_one("#log-progress").display = loading
        self.query_one("#log-progress-label").display = loading
        self.query_one("#tour-panel").display = loading

    def update_progress(self, fraction: float, label: str) -> None:
        bar = self.query_one("#log-progress", ProgressBar)
        if fraction < 0:
            bar.advance(1)   # indeterminate pulse approximation
        else:
            bar.progress = int(fraction * 100)
        self.query_one("#log-progress-label", Static).update(label)

    def update_substage(self, stepper: str, left: str, right: str, dots: str) -> None:
        self.query_one("#log-stepper", Static).update(stepper)
        self.query_one("#tour-left", Static).update(left)
        self.query_one("#tour-right", Static).update(right)
        self.query_one("#tour-panel").display = bool(stepper)

    def append_line(self, line: str) -> None:
        self.query_one("#log-output", RichLog).write(line)

    # Level filter toggles (simple implementation: toggle visibility class)
    def action_toggle_debug(self) -> None:
        self._toggle_level("DEBUG")

    def action_toggle_info(self) -> None:
        self._toggle_level("INFO")

    def action_toggle_warn(self) -> None:
        self._toggle_level("WARN")

    def action_toggle_error(self) -> None:
        self._toggle_level("ERROR")

    def _toggle_level(self, level: str) -> None:
        # RichLog doesn't support per-level filtering natively;
        # this is a UI affordance — in a future iteration we can
        # buffer lines and rebuild. For now, just notify the user.
        self.notify(f"Level filter '{level}' toggled (buffered filtering in future release)")
```

- [ ] **Step 2: Run all tests**

```bash
cd /home/ttuser/code/tt-model-runner-gui
python -m pytest tests/ -q
```

Expected: 32 PASS.

- [ ] **Step 3: Commit**

```bash
git add app/tui/widgets/log_pane.py
git commit -m "feat: add TUI LogPane — stepper, progress, tour panel, RichLog output"
```

---

### Task 11: ConfigPane widget

**Files:**
- Create: `app/tui/widgets/config_pane.py`

ConfigPane is the Config tab: use-case selection, key settings, command preview.

- [ ] **Step 1: Create app/tui/widgets/config_pane.py**

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""ConfigPane — Config tab for the Textual TUI.

Shows use-case chips, key quick settings (context length, max concurrent seqs,
tool use toggle), and a read-only command preview.
"""
from __future__ import annotations

from typing import Callable, Optional, TYPE_CHECKING

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

if TYPE_CHECKING:
    from model_catalog import ModelEntry
    from launch_options import LaunchOptions


_USE_CASE_LABELS = {
    "chat":              "Chat",
    "code_completion":   "Code",
    "agent_frameworks":  "Agent",
    "deep_research":     "Research",
    "creative_writing":  "Creative",
    "dev":               "Dev",
    "smoke_test":        "Smoke",
}


class ConfigPane(Widget):
    """Config tab: use-case selector, quick settings, command preview."""

    DEFAULT_CSS = """
    ConfigPane {
        height: 100%;
        layout: vertical;
        padding: 0 1;
    }
    #model-strip {
        height: 2;
        color: $text-muted;
    }
    #use-case-row {
        height: 3;
        layout: horizontal;
    }
    #quick-settings {
        layout: vertical;
        height: auto;
    }
    #command-preview {
        height: 1fr;
        border: solid $primary-darken-2;
        padding: 0 1;
        color: $text-muted;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._entry: Optional["ModelEntry"] = None
        self._options: Optional["LaunchOptions"] = None
        self._on_options_changed: Optional[Callable] = None

    def compose(self) -> ComposeResult:
        yield Static("Select a model to configure", id="model-strip")
        yield Label("USE CASE")
        yield Widget(id="use-case-row")
        yield Label("QUICK SETTINGS")
        with Widget(id="quick-settings"):
            yield Input(placeholder="Context length (e.g. 131072)", id="ctx-input")
            yield Input(placeholder="Max concurrent seqs (e.g. 1)", id="seq-input")
            yield Checkbox("Enable tool use", id="tool-use-check")
        yield Label("COMMAND PREVIEW")
        yield Static("", id="command-preview")

    def set_model(self, entry: "ModelEntry", on_options_changed: Callable) -> None:
        """Update ConfigPane for a newly selected model entry."""
        from launch_options import LaunchOptions, MODEL_TYPE_USE_CASES, apply_preset

        self._entry = entry
        self._on_options_changed = on_options_changed

        self.query_one("#model-strip", Static).update(
            f"[b]{entry.display_name}[/b]  {entry.model_type} · {entry.inference_engine} · {entry.device_type}"
        )

        # Rebuild use-case buttons
        row = self.query_one("#use-case-row")
        row.remove_children()
        use_cases = MODEL_TYPE_USE_CASES.get(entry.model_type, ["dev"])
        for uc in use_cases:
            label = _USE_CASE_LABELS.get(uc, uc)
            btn = Button(label, id=f"uc-{uc}", variant="default")
            row.mount(btn)

        # Apply default preset
        default_uc = use_cases[0]
        self._options = apply_preset(default_uc, entry)
        self._update_preview()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        if btn_id.startswith("uc-"):
            from launch_options import apply_preset
            uc = btn_id[3:]
            if self._entry:
                self._options = apply_preset(uc, self._entry)
                self._update_preview()
                if self._on_options_changed and self._options:
                    self._on_options_changed(self._options)

    def _update_preview(self) -> None:
        if not self._entry or not self._options:
            return
        from launch_options import build_extra_args

        e = self._entry
        parts = [
            "python3 run.py",
            f"--model {e.display_name}",
            "--workflow server --docker-server",
            "--service-port 8000",
            f"--tt-device {e.device_type.lower()}",
            "--no-auth",
        ]

        class _E:
            inference_engine = e.inference_engine
            family = e.family

        parts += build_extra_args(self._options, _E())
        self.query_one("#command-preview", Static).update(
            " \\\n  ".join(parts)
        )
```

- [ ] **Step 2: Run all tests**

```bash
cd /home/ttuser/code/tt-model-runner-gui
python -m pytest tests/ -q
```

Expected: 32 PASS.

- [ ] **Step 3: Commit**

```bash
git add app/tui/widgets/config_pane.py
git commit -m "feat: add TUI ConfigPane — use-case chips, quick settings, command preview"
```

---

### Task 12: ToolPane and BenchPane widgets

**Files:**
- Create: `app/tui/widgets/tool_pane.py`
- Create: `app/tui/widgets/bench_pane.py`

- [ ] **Step 1: Create app/tui/widgets/tool_pane.py**

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""ToolPane — Tools tab for the Textual TUI.

JSON tool definition editor on the left, prompt + round-trip output on the right.
Only functional when the server was launched with tool_use_enabled=True.
"""
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
            yield TextArea(_SAMPLE_TOOL_JSON, id="tool-def",
                           language="json", theme="monokai")
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
            log.write(f"→ final: {rt.content}")

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
```

- [ ] **Step 2: Create app/tui/widgets/bench_pane.py**

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""BenchPane — Bench tab for the Textual TUI.

Run config (mode selector, sweeps toggle, percentile toggle, run button),
live log output, and a results table showing parsed benchmark results.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Button, Checkbox, DataTable, Label, RichLog, Select, Static

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
    #bench-live-log {
        height: 1fr;
        border: solid $primary-darken-2;
    }
    #bench-results {
        height: 10;
    }
    """

    def compose(self) -> ComposeResult:
        with Widget(id="bench-config-row"):
            yield Label("Mode: ")
            yield Select(_MODES, value="smoke-test", id="bench-mode")
            yield Checkbox("Concurrency sweeps", id="bench-sweeps")
            yield Checkbox("Percentile report",  id="bench-pct")
            yield Button("▶ Run Benchmark", id="bench-run-btn", variant="success")
        yield Label("LIVE OUTPUT")
        yield RichLog(id="bench-live-log", highlight=False, markup=False)
        yield Label("RESULTS")
        yield DataTable(id="bench-results")

    def on_mount(self) -> None:
        table = self.query_one("#bench-results", DataTable)
        table.add_columns("Pass", "ISL", "OSL", "Con", "TTFT ms",
                          "TPS", "E2EL ms", "Req/s", "Timestamp")

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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "bench-run-btn":
            return
        mode   = self.query_one("#bench-mode", Select).value or "smoke-test"
        sweeps = self.query_one("#bench-sweeps", Checkbox).value
        pct    = self.query_one("#bench-pct", Checkbox).value
        self.query_one("#bench-live-log", RichLog).clear()
        app_ctrl = getattr(self.app, "_ctrl", None)
        if app_ctrl:
            app_ctrl.run_benchmark(mode=mode, concurrency_sweeps=sweeps,
                                   percentile_report=pct)
```

- [ ] **Step 3: Run all tests**

```bash
cd /home/ttuser/code/tt-model-runner-gui
python -m pytest tests/ -q
```

Expected: 32 PASS.

- [ ] **Step 4: Commit**

```bash
git add app/tui/widgets/tool_pane.py app/tui/widgets/bench_pane.py
git commit -m "feat: add TUI ToolPane and BenchPane widgets"
```

---

### Task 13: Wire TuiApp config callback + smoke-test the TUI

**Files:**
- Modify: `app/tui/app.py`
- Modify: `app/tui/widgets/model_rail.py` (wire model selection → ConfigPane)

This task wires the remaining inter-widget communication: model selection in the rail triggers `select_model()` on the controller and updates `ConfigPane`; confirms `./run --tui` starts without crashing.

- [ ] **Step 1: Wire model selection in TuiApp._on_catalog_loaded**

In `app/tui/app.py`, update `_on_catalog_loaded` to also subscribe to rail selection changes:

```python
    def _on_catalog_loaded(self, catalog, compatible_devices: list) -> None:
        rail = self.query_one(ModelRail)
        rail.load_catalog(catalog, compatible_devices)
        rail.on_model_select = self._on_model_select

    def _on_model_select(self, entry) -> None:
        self._ctrl.select_model(entry)
        config_pane = self.query_one(ConfigPane)
        port = self.query_one(ModelRail).port_value
        config_pane.set_model(entry, self._on_options_changed)

    def _on_options_changed(self, options) -> None:
        self._ctrl.set_options(options)
```

- [ ] **Step 2: Add on_model_select callback support to ModelRail**

In `app/tui/widgets/model_rail.py`, add `on_model_select` callback attribute and fire it in `on_list_view_selected`:

```python
    # Add after on_launch declaration:
    on_model_select: Optional[Callable] = None

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        entry = getattr(item, "_entry", None)
        if entry is not None:
            self.selected_entry = entry
            if self.on_model_select:
                self.on_model_select(entry)   # ← ADD THIS
```

- [ ] **Step 3: Add import for ConfigPane in tui/app.py**

Ensure `app/tui/app.py` imports `ConfigPane`:

```python
from tui.widgets.config_pane import ConfigPane
```

(It should already be there from Task 8, but verify.)

- [ ] **Step 4: Run all unit tests**

```bash
cd /home/ttuser/code/tt-model-runner-gui
python -m pytest tests/ -q
```

Expected: 32 PASS.

- [ ] **Step 5: Smoke-test TUI startup**

```bash
cd /home/ttuser/code/tt-model-runner-gui
timeout 3 ./run --tui; echo "Exit code: $?"
```

Expected: The TUI starts (you see a brief Textual screen render or the process runs for 3s then gets killed by timeout). Exit code may be 124 (timeout) or 0. The key check is NO Python ImportError or traceback before the timeout.

If there's an ImportError from a missing Textual widget API, fix the import path and re-run.

- [ ] **Step 6: Final test run**

```bash
cd /home/ttuser/code/tt-model-runner-gui
python -m pytest tests/ -v
```

Expected: All 32 tests PASS.

- [ ] **Step 7: Update CLAUDE.md with TUI, tool-calling, and benchmark sections**

Open `CLAUDE.md`. After the existing "Entrypoints" section add (or update existing stubs):

```markdown
## Entrypoints
- `./run` — GTK4 GUI
- `./run --tui` — Textual TUI (same AppController, different view)

## Architecture
- `app/controller.py` — AppController: state machine, all domain logic, no UI imports
- `app/tool_client.py` — Synchronous httpx multi-turn tool-call session
- `app/benchmark_runner.py` — Wraps `tt-inference-server/run.py --workflow benchmarks`
- `app/tui/` — Textual TUI package: app.py, widgets/

## Testing
```bash
pytest tests/         # all unit tests (no display required)
./run                 # GTK GUI (needs display)
./run --tui           # Textual TUI (terminal only)
```

## Threading discipline
All `on_*` callbacks dispatched via `AppController._dispatch` (GLib.idle_add for GTK,
call_from_thread for TUI, sync lambda for tests). Never call widget methods from background threads.

## Benchmark workflow
`AppController.run_benchmark()` → `BenchmarkRunner.run()` → `python3 run.py --workflow benchmarks`
Discovers new `benchmark_*_isl-N_osl-N_maxcon-N.json` files in `workflow_logs/` after subprocess exits.
History saved to `~/.config/tt-runner-gui/benchmarks.json`.
```

- [ ] **Step 8: Final commit**

```bash
git add app/tui/app.py app/tui/widgets/model_rail.py CLAUDE.md
git commit -m "feat: wire TUI model selection → ConfigPane + controller; update CLAUDE.md"
```

---

## Post-Implementation Verification

After all 13 tasks complete, verify:

```bash
# Unit tests
cd /home/ttuser/code/tt-model-runner-gui
python -m pytest tests/ -v

# TUI starts
timeout 5 ./run --tui; echo "TUI exit: $?"

# Check new files exist
ls app/tool_client.py app/benchmark_runner.py \
   app/tui/__init__.py app/tui/app.py \
   app/tui/widgets/{model_rail,log_pane,config_pane,tool_pane,bench_pane}.py
```

All 32+ tests PASS and no ImportErrors on TUI startup = Plan 2 complete.
