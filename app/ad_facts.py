# app/ad_facts.py
# SPDX-License-Identifier: Apache-2.0
"""Ad unit content: static Did-you-know facts and dynamic model recommendations."""
import random
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from compat_catalog import CompatCatalog
    from model_catalog import ModelCatalog

DID_YOU_KNOW: List[Dict[str, str]] = [
    {
        "headline": "Tensix: 108 independent compute cores per chip",
        "body": (
            "Each TT chip packs 108 Tensix cores in a 12×9 grid. "
            "Every core has 1.5 MB local SRAM and a matrix/vector unit that runs "
            "independently — no shared register file, no cache coherence overhead."
        ),
        "tag": "Hardware",
    },
    {
        "headline": "100 Gb/s on-package Ethernet",
        "body": (
            "Chips connect via Ethernet fabric at 100 Gb/s. "
            "An allreduce across 4 chips takes ~1 µs — far less than one transformer "
            "layer's compute time. No proprietary interconnect license."
        ),
        "tag": "Hardware",
    },
    {
        "headline": "TT Metal: zero Python overhead at inference",
        "body": (
            "TT Metal compiles ops into a static kernel-dispatch sequence. "
            "At inference time there is zero Python GIL overhead — the compiled "
            "trace replays RISC-V dispatches directly."
        ),
        "tag": "Software",
    },
    {
        "headline": "tt-forge: any PyTorch model on TT hardware",
        "body": (
            "tt-forge is a torch.compile backend. Any PyTorch model targets TT silicon "
            "without rewriting code — great for CNNs, vision transformers, and research "
            "models that don't need an OpenAI-compatible API server."
        ),
        "tag": "Ecosystem",
    },
    {
        "headline": "Paged KV attention keeps batches full",
        "body": (
            "KV cache is divided into 16-token blocks, eliminating fragmentation. "
            "New requests join mid-generation without stalling existing decodes — "
            "hardware utilization stays high."
        ),
        "tag": "Inference",
    },
    {
        "headline": "Trace capture = JIT compilation",
        "body": (
            "The trace_capture stage compiles separate graphs for 10 context lengths "
            "(128 → 65408 tokens). After capture, every inference replays a pre-built "
            "trace — no re-compilation per request."
        ),
        "tag": "Inference",
    },
    {
        "headline": "Wormhole vs Blackhole generations",
        "body": (
            "Wormhole (N150/N300/T3K) shipped 2022. "
            "Blackhole (P100/P150/P300) shipped 2024 with larger per-core SRAM, "
            "faster interconnect, and better int8 throughput."
        ),
        "tag": "Hardware",
    },
    {
        "headline": "Column-parallel weight sharding",
        "body": (
            "Attention matrices are sharded column-wise across chips: each holds "
            "Q_proj of shape [hidden × (hidden÷N)]. Activations flow over Ethernet "
            "fabric — not through host DRAM — reducing round-trip latency ~100×."
        ),
        "tag": "Architecture",
    },
    {
        "headline": "GQA reduces KV bandwidth 8×",
        "body": (
            "Grouped-Query Attention shares KV heads across query groups. "
            "Llama-3 uses 8 KV heads for 64 query heads — 8× smaller KV cache "
            "and matching reduction in decode memory bandwidth."
        ),
        "tag": "Architecture",
    },
    {
        "headline": "tt-smi: device management in one command",
        "body": (
            "'tt-smi -s' gives a JSON snapshot of temperature, utilization, and firmware. "
            "'tt-smi -r' soft-resets all TT devices — required when switching between "
            "model families that use incompatible memory layouts."
        ),
        "tag": "Workflow",
    },
    {
        "headline": "HF cache: no re-download after first pull",
        "body": (
            "Models cached in ~/.cache/huggingface are mounted read-only into the "
            "Docker container. Weights stream disk→chip at ~7 GB/s via PCIe — "
            "no internet required after the first download."
        ),
        "tag": "Workflow",
    },
    {
        "headline": "Continuous batching keeps utilization high",
        "body": (
            "vLLM fills finished decode slots immediately with new prefills, rather than "
            "waiting for the slowest request in the batch. Chip utilization stays high "
            "even with wildly uneven request lengths."
        ),
        "tag": "Inference",
    },
]


def get_model_cards(
    catalog: Optional["ModelCatalog"],
    device_type: Optional[str],
    compat_catalog: Optional["CompatCatalog"] = None,
) -> List[Dict[str, str]]:
    """Return model-recommendation cards for the current device.

    Sources: existing ModelCatalog (tag='Model') + CompatCatalog for
    tt-forge/tt-metal extras that aren't in the server catalog (tag='Ecosystem').
    """
    cards: List[Dict[str, str]] = []

    if catalog and device_type:
        compatible = catalog.get_compatible([device_type]).all_entries()
        prod = [e for e in compatible if e.status == "PRODUCTION"]
        sample = random.sample(prod, min(4, len(prod))) if prod else compatible[:4]
        for e in sample:
            size_str = f"{e.param_count:.0f}B params  ·  " if e.param_count else ""
            cards.append({
                "headline": e.display_name,
                "body": (
                    f"{size_str}Engine: {e.inference_engine}  ·  "
                    f"Device: {e.device_type}  ·  Status: {e.status}"
                ),
                "tag": "Model",
            })

    if compat_catalog and device_type:
        server_names: set = set()
        if catalog:
            server_names = {e.display_name.lower() for e in catalog.all_entries()}
        extra = []
        for sw in ("tt-forge", "tt-metal"):
            for e in compat_catalog.get_for_hardware(device_type, software=sw):
                if e.display_name.lower() not in server_names and e.model_description:
                    extra.append((e, sw))
        random.shuffle(extra)
        for e, sw in extra[:3]:
            cards.append({
                "headline": e.display_name,
                "body": (
                    f"{e.model_description}\n"
                    f"Available via {sw} on {device_type} — run via Developer Image."
                ),
                "tag": "Ecosystem",
            })

    return cards


def get_all_cards(
    catalog: Optional["ModelCatalog"],
    device_type: Optional[str],
    compat_catalog: Optional["CompatCatalog"] = None,
) -> List[Dict[str, str]]:
    """Return a shuffled pool of static facts + model recommendations."""
    cards = list(DID_YOU_KNOW) + get_model_cards(catalog, device_type, compat_catalog)
    random.shuffle(cards)
    return cards
