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
