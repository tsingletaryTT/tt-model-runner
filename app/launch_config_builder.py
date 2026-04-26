#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build a LaunchConfig from settings + options.

Extracted from AppController._do_launch so ProfileOrchestrator can reuse it.
"""
import copy
import os
from pathlib import Path
from typing import Callable, Optional

from launch_options import LaunchOptions
from server_manager import LaunchConfig


def _read_hf_token(repo_path: Path) -> Optional[str]:
    """Read HF_TOKEN from environment or .env file in repo_path.

    Priority:
      1. HF_TOKEN environment variable (set in shell or injected by the caller)
      2. HF_TOKEN= line in <repo_path>/.env file

    Returns the token string, or None if not found.
    """
    token = os.environ.get("HF_TOKEN", "")
    if token:
        return token
    env_file = repo_path / ".env"
    if env_file.exists():
        for line in env_file.read_text(errors="replace").splitlines():
            if line.startswith("HF_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                if token:
                    return token
    return None


def build_launch_config(
    model_name: str,
    device_type: str,
    port: str,
    device_id: str,
    inference_engine: str,
    settings,
    options: LaunchOptions,
    log_cb: Optional[Callable[[str], None]] = None,
) -> LaunchConfig:
    """Build a LaunchConfig, auto-filling HF token and cache paths from settings.

    Returns a new LaunchConfig. Does not mutate the passed *options* object.
    The device_id is set on an internal copy of options (e.g. "0" or "0,1").

    Cache path priority (first match wins; only one flag may be set per run.py
    contract):
      1. host_weights_dir from settings — most specific, manually downloaded weights
      2. hf_cache_path from settings — reuses already-downloaded HF weights
      3. cache_root_path from settings — generic volume created on first use

    Args:
        model_name: HF repo path or display name passed to run.py --model.
        device_type: Device string, e.g. "N150" or "P300X2".
        port: Service port string, e.g. "8000".
        device_id: Physical device ordinal(s), e.g. "0" or "0,1".
        inference_engine: Engine string, e.g. "vllm" or "media".
        settings: AppSettings instance (or duck-type with same attributes).
        options: LaunchOptions instance to clone; original is never mutated.
        log_cb: Optional callback receiving informational log lines (no \\n).

    Returns:
        A fully populated LaunchConfig ready to pass to ServerManager.launch().
    """
    repo_path = Path(settings.server_repo_path)
    hf_token = _read_hf_token(repo_path)

    # Clone options so the caller's object is never mutated.
    opts = copy.copy(options)
    opts.device_id = device_id

    # Determine whether any cache path is already explicitly set on the cloned
    # options before we apply settings defaults.
    cache_set = bool(opts.host_weights_dir) or bool(opts.host_hf_cache) or bool(opts.host_volume)

    if not cache_set:
        # 1. Explicit weights dir from settings (most specific).
        wd = settings.host_weights_dir
        if wd:
            wd_path = Path(wd).expanduser()
            if wd_path.exists():
                opts.host_weights_dir = str(wd_path)
                cache_set = True
                if log_cb:
                    log_cb(f"ℹ Weights dir → {wd_path}")

    if not cache_set:
        # 2. HF cache — reuses already-downloaded weights, skips re-download.
        hf_cache = settings.hf_cache_path
        if hf_cache:
            hf_dir = Path(hf_cache).expanduser()
            if hf_dir.exists():
                opts.host_hf_cache = str(hf_dir)
                cache_set = True
                if log_cb:
                    log_cb(f"ℹ HF cache  → {hf_dir}")

    if not cache_set:
        # 3. Generic CACHE_ROOT volume (created on first use).
        cache_dir = Path(settings.cache_root_path).expanduser()
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            opts.host_volume = str(cache_dir)
            if log_cb:
                log_cb(f"ℹ Cache dir → {cache_dir}")
        except OSError as exc:
            if log_cb:
                log_cb(f"⚠ Could not create cache dir {cache_dir}: {exc}")

    return LaunchConfig(
        repo_path=repo_path,
        model_name=model_name,
        device=device_type,
        port=port,
        hf_token=hf_token,
        no_auth=True,
        options=opts,
        inference_engine=inference_engine,
    )
