# tests/test_controller.py
"""AppController unit tests — no GTK, no Textual.

Uses NullDispatch (synchronous direct call) so callbacks fire inline and
assertions run immediately after the triggering method.
"""
import sys, os, time, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest
from unittest.mock import MagicMock, patch, call
from controller import AppController
from server_manager import ServerState


# ── Test double ──────────────────────────────────────────────────────────────

class RecordingView:
    """Captures all callback invocations for assertions."""
    def __init__(self):
        self.states: list = []       # [(ServerState, str), ...]
        self.log_lines: list = []    # [str, ...]
        self.progress: list = []     # [(float, str), ...]
        self.substages: list = []    # [(stepper, left, right, dots), ...]

    def on_state_changed(self, state, info): self.states.append((state, info))
    def on_log_line(self, line): self.log_lines.append(line)
    def on_progress(self, fraction, label): self.progress.append((fraction, label))
    def on_substage(self, stepper, left, right, dots): self.substages.append((stepper, left, right, dots))
    def on_catalog_loaded(self, catalog, devices): pass
    def on_cache_scanned(self, info): pass
    def on_bench_progress(self, line): pass
    def on_bench_result(self, result): pass
    def on_tool_result(self, result): pass


def make_controller() -> tuple:
    """Return (AppController, RecordingView) wired with NullDispatch."""
    ctrl = AppController(dispatch_fn=lambda fn, *a: fn(*a))
    view = RecordingView()
    ctrl.on_state_changed  = view.on_state_changed
    ctrl.on_log_line       = view.on_log_line
    ctrl.on_progress       = view.on_progress
    ctrl.on_substage       = view.on_substage
    ctrl.on_catalog_loaded = view.on_catalog_loaded
    ctrl.on_cache_scanned  = view.on_cache_scanned
    ctrl.on_bench_progress = view.on_bench_progress
    ctrl.on_bench_result   = view.on_bench_result
    ctrl.on_tool_result    = view.on_tool_result
    return ctrl, view


# ── State machine ────────────────────────────────────────────────────────────

def test_initial_state_is_idle():
    ctrl, _ = make_controller()
    assert ctrl.state == ServerState.IDLE


def test_vllm_engine_init_log_triggers_loading():
    ctrl, view = make_controller()
    ctrl._handle_log_line("Automatically detected platform tt")
    assert any(s == ServerState.LOADING for s, _ in view.states)


def test_media_server_init_log_triggers_loading():
    ctrl, view = make_controller()
    ctrl._handle_log_line("Creating new Video service")
    assert any(s == ServerState.LOADING for s, _ in view.states)


def test_docker_pull_log_triggers_pulling_image():
    ctrl, view = make_controller()
    ctrl._handle_log_line("docker pull ghcr.io/tenstorrent/tt-inference-server:latest")
    assert any(s == ServerState.PULLING_IMAGE for s, _ in view.states)


def test_no_duplicate_state_transition():
    ctrl, view = make_controller()
    ctrl._handle_log_line("Automatically detected platform tt")
    count_before = len(view.states)
    ctrl._handle_log_line("Automatically detected platform tt")
    # Second identical trigger should not emit a second transition
    assert len(view.states) == count_before


# ── Progress ticks ───────────────────────────────────────────────────────────

def _make_loading_controller():
    ctrl, view = make_controller()
    ctrl._state = ServerState.LOADING
    ctrl._load_start = time.monotonic() - 10.0
    ctrl._current_entry = MagicMock(
        hf_model_repo="meta-llama/Llama-3.1-8B-Instruct",
        device_type="N150",
        inference_engine="vllm",
        min_disk_gb=15.0,
        family="Llama",
    )
    return ctrl, view


def test_trace_capture_progress_fraction():
    ctrl, view = _make_loading_controller()
    # Simulate 5 of 10 trace captures logged
    for seq_len in [128, 256, 512, 1024, 2048]:
        ctrl._handle_log_line(f"Capturing traces: input_seq_len={seq_len}")
    ctrl._progress_tick()
    fractions = [f for f, _ in view.progress]
    assert fractions, "No progress emitted"
    assert abs(fractions[-1] - 0.5) < 0.05, f"Expected ~0.5, got {fractions[-1]}"


def test_warmup_progress_fraction():
    ctrl, view = _make_loading_controller()
    ctrl._server_mgr.parser.warmup_n = 1
    ctrl._server_mgr.parser.warmup_total = 2
    ctrl._progress_tick()
    fractions = [f for f, _ in view.progress]
    assert fractions
    assert abs(fractions[-1] - 0.5) < 0.05


# ── Substage / stepper ───────────────────────────────────────────────────────

def test_substage_emitted_on_log_line():
    ctrl, view = _make_loading_controller()
    ctrl._state = ServerState.LOADING
    ctrl._handle_log_line("Automatically detected platform tt")
    ctrl._handle_log_line("Loading checkpoint shards")
    assert any("Weights" in stepper for stepper, *_ in view.substages), (
        f"Expected 'Weights' in stepper, got: {view.substages}"
    )


def test_stepper_marks_completed_stages():
    ctrl, view = _make_loading_controller()
    ctrl._state = ServerState.LOADING
    ctrl._server_mgr.parser.last_substage = "loading_weights"
    text = ctrl._build_stepper_text("loading_weights")
    assert "✓" in text, "Completed stages should have ✓"
    assert "●" in text, "Active stage should have ●"
    assert "○" in text, "Future stages should have ○"


# ── _emit with dispatch ───────────────────────────────────────────────────────

def test_emit_uses_dispatch_fn():
    dispatched = []
    ctrl = AppController(dispatch_fn=lambda fn, *a: dispatched.append((fn, a)))
    recorded = []
    ctrl.on_log_line = lambda line: recorded.append(line)
    ctrl._emit("on_log_line", "hello")
    # dispatch was called but recorded is still empty (dispatch captured, not called)
    assert len(dispatched) == 1
    assert dispatched[0][1] == ("hello",)


def test_null_dispatch_calls_directly():
    ctrl, view = make_controller()
    ctrl._emit("on_log_line", "test line")
    assert "test line" in view.log_lines


def test_missing_callback_does_not_raise():
    ctrl = AppController()
    # No callbacks registered — _emit should silently do nothing
    ctrl._emit("on_log_line", "ignored")


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


# ── Benchmarks ───────────────────────────────────────────────────────────────

def test_run_benchmark_emits_bench_progress_and_result():
    """run_benchmark() must emit on_bench_progress lines and on_bench_result."""
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

    assert "Running…" in progress_lines
    assert len(results) == 1
    assert results[0].tier_pass == "PASS"
