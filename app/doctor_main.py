#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""tt-model-runner doctor — headless dry-run of the known-issue KB.

Reports which workarounds WOULD apply to a device+model. Mutates nothing:
no .env writes, no launches. Inspired by tt-local-generator's `tt-ctl recover`.

    ./run --doctor --device P100 --model Llama-3.1-8B-Instruct
    ./run --doctor --json          # machine-readable
"""
import argparse
import dataclasses
import json
import sys
from typing import List, Optional

import workaround_resolver as wr
import device_detector


def _tty() -> bool:
    return sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty() else text


def run_doctor(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="tt-model-runner --doctor")
    parser.add_argument("--device", help="DeviceType (P100, N150, …); "
                        "auto-detected via tt-smi if omitted")
    parser.add_argument("--model", default="*",
                        help="Model display name (glob-matched)")
    parser.add_argument("--json", action="store_true",
                        help="Emit matched workarounds as JSON")
    args = parser.parse_args(argv)

    devices = [args.device] if args.device else device_detector.detect_devices()
    if not devices:
        print("No device specified and none detected (tt-smi).", file=sys.stderr)
        return 2

    matched: list = []
    for dev in devices:
        for w in wr.match_preflight(dev, args.model, None):
            matched.append((dev, w))

    if args.json:
        print(json.dumps([dataclasses.asdict(w) for _, w in matched], indent=2))
        return 1 if matched else 0

    if not matched:
        print(_c("32", "✓") + f"  No known issues for {', '.join(devices)} + {args.model}")
        return 0

    for dev, w in matched:
        print(_c("33", "●") + f"  {dev} + {args.model}")
        print(f"  known issue {w.ref or w.id}")
        changes = []
        if w.env:
            changes.append(", ".join(f"{k}={v}" for k, v in w.env.items()))
        if "max_model_len" in w.vllm:
            changes.append(f"max_model_len={w.vllm['max_model_len']}")
        if changes:
            print(f"  would apply: {'; '.join(changes)}")
        if w.tradeoff:
            print("  " + _c("33", f"⚠ {w.tradeoff}"))
    # Guidance to stderr so stdout stays parseable.
    print("\n(dry-run — launch the model to auto-apply)", file=sys.stderr)
    return 1


def main() -> None:
    sys.exit(run_doctor(sys.argv[1:]))


if __name__ == "__main__":
    main()
