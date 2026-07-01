# tests/test_controller.py
"""AppController unit tests — no GTK, no Textual.

Uses NullDispatch (synchronous direct call) so callbacks fire inline and
assertions run immediately after the triggering method.
"""
import sys, os, time, threading
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest
from unittest.mock import MagicMock, patch, call
from controller import AppController, BenchResult
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


# ── Error recovery hints ─────────────────────────────────────────────────────

def test_error_hint_emitted_on_known_pattern():
    ctrl, view = make_controller()
    ctrl._handle_log_line("Cannot connect to the Docker daemon at unix:///var/run/docker.sock")
    hints = [l for l in view.log_lines if l.startswith("💡")]
    assert hints, "Expected a 💡 hint for Docker daemon error"
    assert "docker" in hints[0].lower() or "Docker" in hints[0]


def test_error_hint_emitted_only_once_per_run():
    ctrl, view = make_controller()
    ctrl._handle_log_line("Cannot connect to the Docker daemon")
    ctrl._handle_log_line("Cannot connect to the Docker daemon")
    hints = [l for l in view.log_lines if l.startswith("💡")]
    assert len(hints) == 1, "Duplicate hints should be suppressed within a run"


def test_error_hints_reset_on_new_launch():
    ctrl, view = make_controller()
    ctrl._handle_log_line("Cannot connect to the Docker daemon")
    count_before = len([l for l in view.log_lines if l.startswith("💡")])
    # Simulate a new launch cycle resetting the emitted set
    ctrl._emitted_error_hints.clear()
    ctrl._handle_log_line("Cannot connect to the Docker daemon")
    hints_after = [l for l in view.log_lines if l.startswith("💡")]
    assert len(hints_after) > count_before, "Hint should re-emit after reset"


def test_unknown_error_line_emits_no_hint():
    ctrl, view = make_controller()
    ctrl._handle_log_line("Some random line with no known pattern XYZ123")
    hints = [l for l in view.log_lines if l.startswith("💡")]
    assert not hints, "No hint for unrecognized log lines"


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


# ── Docker pull progress ──────────────────────────────────────────────────────

def test_pull_done_increments_counter():
    ctrl, _ = make_controller()
    ctrl._state = ServerState.PULLING_IMAGE
    assert ctrl._pull_layers_done == 0
    ctrl._update_pull_progress("abc123def456: Pull complete")
    assert ctrl._pull_layers_done == 1


def test_pull_downloading_updates_dict():
    ctrl, _ = make_controller()
    ctrl._state = ServerState.PULLING_IMAGE
    line = "abc123def456: Downloading [==>  ] 123.4MB/1.5GB"
    result = ctrl._update_pull_progress(line)
    assert "abc123def456" in ctrl._pull_downloading
    cur, tot = ctrl._pull_downloading["abc123def456"]
    assert abs(cur - 123.4e6) < 1e5
    assert abs(tot - 1.5e9) < 1e7


def test_pull_summary_includes_layers_done():
    ctrl, _ = make_controller()
    ctrl._state = ServerState.PULLING_IMAGE
    ctrl._update_pull_progress("abc123def456: Pull complete")
    ctrl._update_pull_progress("def456abc123: Pull complete")
    summary = ctrl._update_pull_progress("no pull here")
    assert "2 layers done" in summary


def test_pull_summary_empty_before_any_progress():
    ctrl, _ = make_controller()
    ctrl._state = ServerState.PULLING_IMAGE
    summary = ctrl._update_pull_progress("Pulling from ghcr.io/tenstorrent/tt-inference-server")
    assert summary == ""


def test_pull_summary_emitted_as_substage_via_handle_log():
    ctrl, view = make_controller()
    ctrl._state = ServerState.PULLING_IMAGE
    ctrl._handle_log_line("abc123def456: Pull complete")
    substages = [s for s in view.substages if s[0] == "⬇"]
    assert substages, "Expected pull substage to be emitted"
    assert "1 layers done" in substages[-1][1]


def test_pull_counters_reset_on_entering_pulling_state():
    ctrl, _ = make_controller()
    # Simulate partial pull state, then a fresh PULLING_IMAGE transition from LAUNCHING
    ctrl._state = ServerState.LAUNCHING
    ctrl._pull_layers_done = 5
    ctrl._pull_downloading = {"abc": (1e6, 2e6)}
    ctrl._transition(ServerState.PULLING_IMAGE)
    assert ctrl._pull_layers_done == 0
    assert ctrl._pull_downloading == {}


# ── Benchmark history persistence ──────────────────────────────────────────────

def _make_test_settings(tmp_path):
    from app_settings import AppSettings
    return AppSettings(config_dir=tmp_path)


def test_persist_bench_result_appends_to_settings(tmp_path):
    """_persist_bench_result should append a serialized entry to settings."""
    fake_settings = _make_test_settings(tmp_path)
    ctrl, _ = make_controller()
    with patch("controller._settings", fake_settings):
        r = BenchResult(
            model_name="llama-3-8b", device="N150",
            timestamp="2026-04-19T10:00:00",
            isl=128, osl=128, concurrency=1,
            mean_ttft_ms=80.0, p95_ttft_ms=None,
            mean_tps=35.0, tps_decode=36.0,
            mean_e2el_ms=900.0, request_throughput=0.8,
            tier_pass="PASS",
        )
        ctrl._persist_bench_result(r)
        history = fake_settings.benchmark_history
        assert len(history) == 1
        assert history[0]["model_name"] == "llama-3-8b"
        assert history[0]["tier_pass"] == "PASS"


def test_get_bench_history_newest_first(tmp_path):
    fake_settings = _make_test_settings(tmp_path)
    ctrl, _ = make_controller()
    with patch("controller._settings", fake_settings):
        for i in range(3):
            r = BenchResult(
                model_name=f"model-{i}", device="N150",
                timestamp=f"2026-04-19T1{i}:00:00",
                isl=128, osl=128, concurrency=1,
                mean_ttft_ms=80.0, p95_ttft_ms=None,
                mean_tps=35.0, tps_decode=36.0,
                mean_e2el_ms=900.0, request_throughput=0.8,
                tier_pass="PASS",
            )
            ctrl._persist_bench_result(r)
        history = ctrl.get_bench_history()
        assert len(history) == 3
        assert history[0]["model_name"] == "model-2"   # newest first
        assert history[-1]["model_name"] == "model-0"


# ── Bench history clear ────────────────────────────────────────────────────────

def test_clear_bench_history_empties_settings(tmp_path):
    """clear_bench_history should wipe benchmark_history in settings."""
    fake_settings = _make_test_settings(tmp_path)
    ctrl, _ = make_controller()
    with patch("controller._settings", fake_settings):
        r = BenchResult(
            model_name="llama-3-8b", device="N150",
            timestamp="2026-04-19T10:00:00",
            isl=128, osl=128, concurrency=1,
            mean_ttft_ms=80.0, p95_ttft_ms=None,
            mean_tps=35.0, tps_decode=36.0,
            mean_e2el_ms=900.0, request_throughput=0.8,
            tier_pass="PASS",
        )
        ctrl._persist_bench_result(r)
        assert len(fake_settings.benchmark_history) == 1
        ctrl.clear_bench_history()
        assert fake_settings.benchmark_history == []
        assert ctrl.get_bench_history() == []


# ── get_repo_git_info ─────────────────────────────────────────────────────────

def test_get_repo_git_info_returns_empty_for_non_git_path(tmp_path):
    """Non-git directories return ('', '')."""
    ctrl, _ = make_controller()
    branch, sha = ctrl.get_repo_git_info(path=tmp_path)
    assert branch == ""
    assert sha == ""


def test_get_repo_git_info_handles_missing_git_gracefully():
    """If git is not on PATH, should return ('', '') without raising."""
    import subprocess
    ctrl, _ = make_controller()
    with patch("subprocess.check_output", side_effect=FileNotFoundError):
        branch, sha = ctrl.get_repo_git_info()
    assert branch == ""
    assert sha == ""


# ── pull_repo ────────────────────────────────────────────────────────────────

def test_pull_repo_emits_log_lines_and_calls_on_complete(tmp_path):
    """pull_repo should emit log lines and invoke on_complete(bool, str)."""
    import subprocess
    ctrl, view = make_controller()

    mock_proc = MagicMock()
    mock_proc.stdout = iter(["Already up to date.\n"])
    mock_proc.wait.return_value = 0
    fake_settings = _make_test_settings(tmp_path)
    fake_settings.server_repo_path = str(tmp_path)

    completed = []
    with patch("subprocess.Popen", return_value=mock_proc), \
         patch("controller._settings", fake_settings):
        ctrl.pull_repo(on_complete=lambda ok, msg: completed.append((ok, msg)))
        # pull_repo runs in a daemon thread; wait briefly for it
        import time
        for _ in range(50):
            if completed:
                break
            time.sleep(0.02)

    assert completed, "on_complete was never called"
    ok, msg = completed[0]
    assert ok is True
    log_text = " ".join(view.log_lines)
    assert "git pull" in log_text.lower()


# ── Model starring ───────────────────────────────────────────────────────────

def _make_mock_entry(model_name="llama-3-8b", device="N150"):
    entry = MagicMock()
    entry.model_name = model_name
    entry.device_type = device
    return entry


def test_toggle_star_stars_unstarred_model(tmp_path):
    fake_settings = _make_test_settings(tmp_path)
    ctrl, _ = make_controller()
    with patch("controller._settings", fake_settings):
        entry = _make_mock_entry()
        assert not ctrl.is_starred(entry)
        result = ctrl.toggle_star(entry)
        assert result is True
        assert ctrl.is_starred(entry)


def test_toggle_star_unstars_starred_model(tmp_path):
    fake_settings = _make_test_settings(tmp_path)
    ctrl, _ = make_controller()
    with patch("controller._settings", fake_settings):
        entry = _make_mock_entry()
        ctrl.toggle_star(entry)   # star
        result = ctrl.toggle_star(entry)  # unstar
        assert result is False
        assert not ctrl.is_starred(entry)


def test_star_persists_across_toggle(tmp_path):
    fake_settings = _make_test_settings(tmp_path)
    ctrl, _ = make_controller()
    with patch("controller._settings", fake_settings):
        e1 = _make_mock_entry("model-a", "N150")
        e2 = _make_mock_entry("model-b", "N300")
        ctrl.toggle_star(e1)
        ctrl.toggle_star(e2)
        ctrl.toggle_star(e1)  # unstar model-a
        assert not ctrl.is_starred(e1)
        assert ctrl.is_starred(e2)


# ── Per-model options persistence ────────────────────────────────────────────

def test_set_options_persists_non_default_fields(tmp_path):
    """set_options should write non-default fields to model_options in settings."""
    from launch_options import LaunchOptions
    fake_settings = _make_test_settings(tmp_path)
    ctrl, _ = make_controller()
    with patch("controller._settings", fake_settings):
        ctrl._current_entry = _make_mock_entry("llama-3-8b", "N150")
        opts = LaunchOptions(use_case="chat", max_model_len=32768)
        ctrl.set_options(opts)
        saved = fake_settings.model_options
        assert "llama-3-8b" in saved
        assert saved["llama-3-8b"]["max_model_len"] == 32768


def test_set_options_removes_entry_when_all_defaults(tmp_path):
    """set_options with all-default values should delete the model's stored entry."""
    from launch_options import LaunchOptions
    fake_settings = _make_test_settings(tmp_path)
    ctrl, _ = make_controller()
    with patch("controller._settings", fake_settings):
        ctrl._current_entry = _make_mock_entry("llama-3-8b", "N150")
        # First save something, then reset to defaults
        fake_settings.model_options = {"llama-3-8b": {"max_model_len": 32768}}
        ctrl.set_options(LaunchOptions())   # all defaults
        saved = fake_settings.model_options
        assert "llama-3-8b" not in saved


def test_select_model_restores_saved_options(tmp_path):
    """select_model should restore non-default fields from model_options."""
    from launch_options import LaunchOptions
    fake_settings = _make_test_settings(tmp_path)
    fake_settings.model_options = {
        "llama-3-8b": {"use_case": "code_completion", "max_model_len": 32768}
    }
    ctrl, _ = make_controller()
    with patch("controller._settings", fake_settings):
        entry = _make_mock_entry("llama-3-8b", "N150")
        entry.hf_model_repo = "meta-llama/Llama-3-8B"
        ctrl.select_model(entry)
        # Give the background cache scan thread time to start (it's daemon, doesn't block)
        opts = ctrl.get_options()
        assert opts.use_case == "code_completion"
        assert opts.max_model_len == 32768


# ── Known-issue auto-remediation (_apply_remedy / undo_remediation) ────────────

import workaround_resolver as wr


def test_apply_remedy_writes_env_and_sets_override(tmp_path, monkeypatch):
    ctrl, _ = make_controller()
    fake_settings = _make_test_settings(tmp_path)
    fake_settings.server_repo_path = str(tmp_path)
    w = wr.Workaround(
        id="p100", devices=["P100"], models=["Llama-3.1-8B*"],
        symptom="clash with L1 buffers", env={"MAX_PREFILL_CHUNK_SIZE": "2"},
        vllm={"max_model_len": 1024}, also_move_to_env=["HF_TOKEN"],
        tradeoff="context capped", ref="tt-metal#28835",
    )
    monkeypatch.setenv("HF_TOKEN", "hf_secret")

    with patch("controller._settings", fake_settings):
        applied = ctrl._apply_remedy(w, Path(tmp_path))

        env_text = (tmp_path / ".env").read_text()
        assert "MAX_PREFILL_CHUNK_SIZE=2" in env_text
        assert "HF_TOKEN=hf_secret" in env_text          # relocated from environment
        assert ctrl._options.max_model_len == 1024
        assert ctrl._applied_remedy is applied
        assert "MAX_PREFILL_CHUNK_SIZE" in applied.env_keys_written


def test_undo_remediation_scrubs_and_clears(tmp_path):
    ctrl, _ = make_controller()
    fake_settings = _make_test_settings(tmp_path)
    fake_settings.server_repo_path = str(tmp_path)
    w = wr.Workaround(id="p100", devices=["P100"], models=["*"],
                       env={"MAX_PREFILL_CHUNK_SIZE": "2"}, vllm={"max_model_len": 1024})

    with patch("controller._settings", fake_settings):
        ctrl._apply_remedy(w, Path(tmp_path))

        ctrl.undo_remediation()

        assert "MAX_PREFILL_CHUNK_SIZE" not in (tmp_path / ".env").read_text()
        assert ctrl._options.max_model_len is None
        assert ctrl._applied_remedy is None


# ── Pre-flight hook (_preflight_apply) ──────────────────────────────────────────

def test_preflight_applies_remedy_before_launch(tmp_path):
    """_preflight_apply should apply matching auto remedies before _do_launch runs."""
    ctrl, _ = make_controller()
    fake_settings = _make_test_settings(tmp_path)
    fake_settings.server_repo_path = str(tmp_path)
    (tmp_path / "run.py").write_text("# stub")

    entry = MagicMock()
    entry.display_name = "Llama-3.1-8B-Instruct"
    entry.device_type = "P100"
    entry.inference_engine = "vllm"

    w = wr.Workaround(id="p100", devices=["P100"], models=["*"],
                       env={"MAX_PREFILL_CHUNK_SIZE": "2"}, vllm={"max_model_len": 1024})

    captured = {}
    with patch("controller._settings", fake_settings), \
         patch("controller._wr.match_preflight",
               lambda device, model, repo_version=None: [w]), \
         patch.object(ctrl, "_do_launch",
                      lambda entry, port: captured.update(mml=ctrl._options.max_model_len)):
        ctrl._preflight_apply(entry)
        ctrl._do_launch(entry, "8000")

    assert captured["mml"] == 1024
    assert "MAX_PREFILL_CHUNK_SIZE=2" in (tmp_path / ".env").read_text()


def test_preflight_apply_skips_launch_block_on_resolver_error(tmp_path):
    """A resolver exception must never propagate — pre-flight fails open."""
    ctrl, _ = make_controller()
    fake_settings = _make_test_settings(tmp_path)
    fake_settings.server_repo_path = str(tmp_path)

    entry = MagicMock()
    entry.display_name = "Llama-3.1-8B-Instruct"
    entry.device_type = "P100"
    entry.inference_engine = "vllm"

    with patch("controller._settings", fake_settings), \
         patch("controller._wr.match_preflight", side_effect=RuntimeError("kb boom")):
        ctrl._preflight_apply(entry)   # must not raise

    assert ctrl._applied_remedy is None


def test_preflight_apply_warns_but_does_not_apply_non_auto_remedy(tmp_path):
    """auto: false remedies should log a warning, not be silently applied."""
    ctrl, view = make_controller()
    fake_settings = _make_test_settings(tmp_path)
    fake_settings.server_repo_path = str(tmp_path)

    entry = MagicMock()
    entry.display_name = "Llama-3.1-8B-Instruct"
    entry.device_type = "P100"
    entry.inference_engine = "vllm"

    w = wr.Workaround(id="manual-fix", devices=["P100"], models=["*"],
                       env={"SOME_FLAG": "1"}, auto=False,
                       tradeoff="requires manual review", ref="tt-metal#99999")

    with patch("controller._settings", fake_settings), \
         patch("controller._wr.match_preflight",
               lambda device, model, repo_version=None: [w]):
        ctrl._preflight_apply(entry)

    assert ctrl._applied_remedy is None
    assert not (tmp_path / ".env").exists()
    assert any("tt-metal#99999" in line or "manual-fix" in line
               for line in view.log_lines)
