#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Read model architecture facts and file listings from the HF hub cache."""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class ArchFacts:
    model_type: str = ""
    num_layers: int = 0
    hidden_size: int = 0
    num_heads: int = 0
    num_kv_heads: int = 0
    head_dim: int = 0
    context_length: int = 0
    vocab_size: int = 0
    intermediate_size: int = 0


@dataclass
class CachedFile:
    name: str
    size_bytes: int

    @property
    def size_str(self) -> str:
        b = self.size_bytes
        if b >= 1_000_000_000:
            return f"{b / 1e9:.1f}GB"
        if b >= 1_000_000:
            return f"{b / 1e6:.0f}MB"
        return f"{b / 1e3:.0f}KB"


@dataclass
class ModelCacheInfo:
    hf_repo: str
    is_cached: bool = False
    arch: Optional[ArchFacts] = None
    safetensors: List[CachedFile] = field(default_factory=list)
    other_files: List[CachedFile] = field(default_factory=list)
    total_bytes: int = 0


def _repo_to_cache_name(hf_repo: str) -> str:
    """meta-llama/Llama-3.3-70B-Instruct → models--meta-llama--Llama-3.3-70B-Instruct"""
    return "models--" + hf_repo.replace("/", "--")


def _find_snapshot(hf_repo: str) -> Optional[Path]:
    """Return the most recently modified snapshot directory, or None if not cached."""
    cache_root = Path.home() / ".cache" / "huggingface" / "hub"
    snapshots = cache_root / _repo_to_cache_name(hf_repo) / "snapshots"
    if not snapshots.is_dir():
        return None
    dirs = [p for p in snapshots.iterdir() if p.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda p: p.stat().st_mtime)


def _parse_arch(config_path: Path) -> Optional[ArchFacts]:
    try:
        cfg = json.loads(config_path.read_text())
    except Exception:
        return None
    a = ArchFacts()
    a.model_type = cfg.get("model_type", "")
    a.num_layers = (
        cfg.get("num_hidden_layers") or cfg.get("n_layer") or
        cfg.get("num_layers") or cfg.get("depth") or 0
    )
    a.hidden_size = (
        cfg.get("hidden_size") or cfg.get("d_model") or cfg.get("n_embd") or 0
    )
    a.num_heads = cfg.get("num_attention_heads") or cfg.get("n_head") or 0
    a.num_kv_heads = cfg.get("num_key_value_heads") or a.num_heads
    a.context_length = (
        cfg.get("max_position_embeddings") or cfg.get("max_seq_len") or
        cfg.get("seq_length") or cfg.get("max_length") or 0
    )
    a.vocab_size = cfg.get("vocab_size") or 0
    a.intermediate_size = cfg.get("intermediate_size") or cfg.get("ffn_dim") or 0
    if a.num_heads and a.hidden_size:
        a.head_dim = a.hidden_size // a.num_heads
    return a


def scan_all_cached(hf_repos) -> set:
    """Return the subset of hf_repos that have a local HF cache snapshot. Fast disk check only."""
    return {repo for repo in hf_repos if _find_snapshot(repo) is not None}


def scan_model_cache(hf_repo: str) -> ModelCacheInfo:
    """Scan HF cache for model files and architecture facts. Call from a worker thread."""
    info = ModelCacheInfo(hf_repo=hf_repo)
    snap = _find_snapshot(hf_repo)
    if not snap:
        return info
    info.is_cached = True
    config_path = snap / "config.json"
    if config_path.exists():
        info.arch = _parse_arch(config_path)
    total = 0
    for p in sorted(snap.iterdir()):
        if not p.is_file():
            continue
        try:
            sz = p.stat().st_size
        except OSError:
            continue
        total += sz
        cf = CachedFile(name=p.name, size_bytes=sz)
        if p.suffix == ".safetensors":
            info.safetensors.append(cf)
        else:
            info.other_files.append(cf)
    info.total_bytes = total
    return info
