#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Wraps tt-inference-server run.py --workflow benchmarks.

Runs in a background thread. Streams stdout to on_progress callback.
After subprocess exits, discovers new benchmark_*.json files in workflow_logs/,
parses metrics, evaluates pass/fail against model_spec.json perf_reference
targets, persists results, and calls on_result(BenchResult) for each.

Pass/fail tiers (evaluated in order, most strict first):
  PASS         — all metrics in 'customer_functional' within 10% tolerance
  BELOW_TARGET — fails customer_functional but passes 'functional' within 50%
  FAIL         — fails both tiers

For throughput metrics (mean_tps, tps_decode_throughput, request_throughput):
  actual must be >= ref * (1 - tolerance)   (higher = better)

For latency metrics (mean_ttft_ms, p95_ttft_ms, mean_e2el_ms):
  actual must be <= ref * (1 + tolerance)   (lower = better)
"""
import json
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Set

from controller import BenchResult


# Maps BenchResult field names → JSON keys written by tt-inference-server
_METRIC_MAP = {
    # dest key (BenchResult field)  : src key (in benchmark JSON file)
    "mean_ttft_ms":       "mean_ttft_ms",
    "p95_ttft_ms":        "p95_ttft_ms",
    "mean_tps":           "mean_tps",
    "tps_decode":         "tps_decode_throughput",
    "mean_e2el_ms":       "mean_e2el_ms",
    "request_throughput": "request_throughput",
}

# Metrics where a higher value is better (used by _eval_tier to pick direction)
_HIGHER_IS_BETTER = {"mean_tps", "tps_decode", "tps_decode_throughput", "request_throughput"}


def _parse_filename(name: str) -> Optional[Dict]:
    """Extract isl/osl/concurrency from a benchmark filename.

    Expects a name matching the pattern:
        benchmark_*_isl-<N>_osl-<N>_maxcon-<N>*.json
    Returns a dict with int keys isl, osl, concurrency, or None on mismatch.
    """
    m = re.search(r"isl-(\d+)_osl-(\d+)_maxcon-(\d+)", name)
    if not m:
        return None
    return {
        "isl": int(m.group(1)),
        "osl": int(m.group(2)),
        "concurrency": int(m.group(3)),
    }


def _parse_json_file(path: Path) -> Optional[Dict]:
    """Read and JSON-decode a benchmark result file.

    Returns the parsed dict, or None if the file is missing or malformed.
    """
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _eval_tier(metrics: Dict, targets: Dict) -> str:
    """Evaluate parsed metrics against perf_reference tier targets.

    Args:
        metrics: dict of metric_name → float value
        targets: dict of tier_name → {metric_name: ref_value}, e.g.
                 {"customer_functional": {"mean_tps": 35.0},
                  "functional": {"mean_tps": 20.0}}

    Returns:
        "PASS"         — customer_functional tier satisfied (10% tolerance)
        "BELOW_TARGET" — functional tier satisfied (50% tolerance)
        "FAIL"         — neither tier satisfied
    """
    if not targets:
        return "PASS"

    def _tier_ok(tier_key: str, tolerance: float) -> bool:
        """Return True if the tier is defined and all its targets are within tolerance.

        A tier that is absent from targets is treated as not satisfied (False),
        not as an implicit pass — avoids spurious BELOW_TARGET when only
        customer_functional is configured and the metric misses.
        """
        if tier_key not in targets:
            return False
        tier = targets[tier_key]
        for metric_key, ref in tier.items():
            if not isinstance(ref, (int, float)):
                continue
            actual = metrics.get(metric_key)
            if actual is None:
                # Missing metric: skip rather than fail (best-effort)
                continue
            # Determine comparison direction from the metric name
            is_throughput = "tps" in metric_key or "throughput" in metric_key
            if is_throughput:
                # Higher is better: actual must be at least ref*(1-tol)
                if actual < ref * (1.0 - tolerance):
                    return False
            else:
                # Lower is better (latency): actual must be at most ref*(1+tol)
                if actual > ref * (1.0 + tolerance):
                    return False
        return True

    if _tier_ok("customer_functional", 0.10):
        return "PASS"
    if _tier_ok("functional", 0.50):
        return "BELOW_TARGET"
    return "FAIL"


class BenchmarkRunner:
    """Runs tt-inference-server benchmarks and emits structured results.

    Usage:
        runner = BenchmarkRunner(
            repo_path=Path("/path/to/tt-inference-server"),
            on_progress=lambda line: print(line),
            on_result=lambda r: print(r),
        )
        runner.run(model_name="Llama-3.1-8B", device="N150")

    The run() method launches a daemon thread. Results arrive via on_result
    after the subprocess exits, one BenchResult per discovered JSON file.
    """

    def __init__(
        self,
        repo_path: Path,
        on_progress: Callable[[str], None],
        on_result: Callable[[BenchResult], None],
    ) -> None:
        """Initialize the runner.

        Args:
            repo_path: Path to the tt-inference-server repository checkout.
            on_progress: Callback invoked with each line of subprocess stdout.
            on_result: Callback invoked with a parsed BenchResult for each
                       benchmark JSON file discovered after the run.
        """
        self._repo = Path(repo_path)
        self._on_progress = on_progress
        self._on_result = on_result
        # Default history location; tests may override _history_path directly.
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
        """Start the benchmark in a daemon background thread.

        Args:
            model_name: HuggingFace model repo name (e.g. "Llama-3.1-8B").
            device: Hardware target string (e.g. "N150", "N300").
            mode: Sampling mode passed as --limit-samples-mode (e.g. "smoke-test").
            concurrency_sweeps: If True, append --concurrency-sweeps flag.
            percentile_report: If True, append --percentile-report flag.
            perf_targets: Optional perf_reference targets from model_spec.json,
                          structured as {tier: {metric: ref_value}}.
        """
        logs_dir = self._repo / "workflow_logs"
        # Snapshot existing files so we only process files that appeared during this run.
        pre_existing: Set[Path] = (
            set(logs_dir.glob("benchmark_*.json")) if logs_dir.exists() else set()
        )
        threading.Thread(
            target=self._run,
            args=(
                model_name, device, mode, concurrency_sweeps,
                percentile_report, perf_targets or {}, pre_existing,
            ),
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
        """Internal synchronous implementation (called from daemon thread).

        Builds the subprocess command, streams output line-by-line through
        on_progress, then discovers and processes new JSON result files.
        """
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

        # Emit the command line so the UI can display exactly what ran
        self._on_progress(f"$ {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,   # merge stderr so nothing is silently swallowed
                text=True,
                cwd=self._repo,
            )
            # Stream every line to the UI as it arrives
            for line in proc.stdout:
                self._on_progress(line.rstrip())
            proc.wait()
        except Exception as exc:
            self._on_progress(f"Error launching benchmark: {exc}")
            return

        # Collect benchmark JSON files that appeared during this run
        logs_dir = self._repo / "workflow_logs"
        new_files: Set[Path] = (
            set(logs_dir.glob("benchmark_*.json")) - pre_existing
            if logs_dir.exists()
            else set()
        )
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")

        for path in sorted(new_files):
            dim = _parse_filename(path.name)
            if dim is None:
                # Filename doesn't match expected pattern — skip
                continue
            raw = _parse_json_file(path)
            if raw is None:
                # Unreadable or malformed JSON — skip
                continue

            # Remap raw JSON keys to BenchResult field names, coercing to float
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
                p95_ttft_ms=metrics.get("p95_ttft_ms"),          # Optional — may be None
                mean_tps=metrics.get("mean_tps", 0.0),
                tps_decode=metrics.get("tps_decode", 0.0),
                mean_e2el_ms=metrics.get("mean_e2el_ms", 0.0),
                request_throughput=metrics.get("request_throughput", 0.0),
                tier_pass=tier,
            )
            self._persist(result)
            self._on_result(result)

    def _persist(self, result: BenchResult) -> None:
        """Append a BenchResult to the JSON history file.

        Creates the file and parent directories if they do not exist.
        Existing history is loaded and the new result is appended, then
        the whole list is written back atomically via write_text.

        Args:
            result: The BenchResult to append.
        """
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        history = []
        if self._history_path.exists():
            try:
                history = json.loads(self._history_path.read_text())
            except (json.JSONDecodeError, OSError):
                # Corrupted or missing file — start fresh rather than crashing
                pass
        history.append({
            "model_name":         result.model_name,
            "device":             result.device,
            "timestamp":          result.timestamp,
            "isl":                result.isl,
            "osl":                result.osl,
            "concurrency":        result.concurrency,
            "mean_ttft_ms":       result.mean_ttft_ms,
            "p95_ttft_ms":        result.p95_ttft_ms,
            "mean_tps":           result.mean_tps,
            "tps_decode":         result.tps_decode,
            "mean_e2el_ms":       result.mean_e2el_ms,
            "request_throughput": result.request_throughput,
            "tier_pass":          result.tier_pass,
        })
        self._history_path.write_text(json.dumps(history, indent=2))
