#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""SavedConfig dataclass — snapshot of a successful launch configuration.

A SavedConfig captures everything needed to reproduce a server launch so the
app can restore it on next startup (Last Successful Deployment) or let the user
save named favourites.

For single-server launches, all fields are populated.
For multi-slot deploy profiles, model_name / device_type / port / docker_image
are left as empty strings and deploy_profile_name identifies which profile to
load from the DeployProfileStore.

Typical usage:
    cfg = SavedConfig(
        name="__last_success__",
        model_name="Llama-3.1-8B",
        device_type="n300",
        port=8000,
        docker_image="ghcr.io/tenstorrent/tt-inference-server:v0.0.1",
        inference_engine="vllm",
        options_json=json.dumps(dataclasses.asdict(launch_options)),
        deploy_profile_name="",
        created=now_iso(),
        last_used=now_iso(),
    )
    store.save(cfg)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SavedConfig:
    """Snapshot of a successful (or intended) launch configuration.

    Fields
    ------
    name : str
        Human-readable identifier.  Use ``"__last_success__"`` for the
        auto-saved last-good launch and any other string for user-chosen
        favourite names.

    model_name : str
        Display name from the model catalog (e.g. ``"Llama-3.1-8B"``).
        Set to ``""`` when the config is tied to a deploy profile rather than
        a single model.

    device_type : str
        Hardware target string used by the launcher (e.g. ``"n300"``).
        Set to ``""`` for profile-only configs.

    port : int
        Host port the inference server listens on.
        Set to ``0`` for profile-only configs.

    docker_image : str
        Fully-qualified Docker image tag
        (e.g. ``"ghcr.io/tenstorrent/tt-inference-server:v0.0.1"``).
        Set to ``""`` for profile-only configs.

    inference_engine : str
        Engine identifier: ``"vllm"``, ``"tt-transformers"``, or ``""`` for
        profile-only configs.

    options_json : str
        ``json.dumps(dataclasses.asdict(launch_options))`` — full serialisation
        of the :class:`~launch_options.LaunchOptions` used at launch time.
        Stored as a JSON string so SavedConfig itself remains a flat dataclass
        with no nested mutable fields (makes equality checks and persistence
        straightforward).

    deploy_profile_name : str
        Name of the :class:`~deploy_profile.DeployProfile` that was active when
        this config was saved.  Empty string for single-server launches.

    created : str
        ISO-8601 timestamp of the first time this config was saved
        (``"2026-04-27T12:00:00"``).  Set once and never updated.

    last_used : str
        ISO-8601 timestamp updated every time this config is restored or
        re-launched.
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    name: str               # "__last_success__" or user-chosen name

    # ── Single-server fields (all "" / 0 for profile-only configs) ────────────
    model_name: str         # display name from catalog ("" for profile-only)
    device_type: str        # e.g. "n300" ("" for profile-only)
    port: int               # 0 for profile-only
    docker_image: str       # full tag or "" for profile-only
    inference_engine: str   # "vllm" | "tt-transformers" | ""

    # ── Launch options snapshot ───────────────────────────────────────────────
    options_json: str       # json.dumps(dataclasses.asdict(LaunchOptions))

    # ── Multi-slot deploy profile ─────────────────────────────────────────────
    deploy_profile_name: str  # "" for single-server; profile name for multi-slot

    # ── Timestamps ────────────────────────────────────────────────────────────
    created: str            # ISO timestamp, set on first save
    last_used: str          # ISO timestamp, updated on every restore
