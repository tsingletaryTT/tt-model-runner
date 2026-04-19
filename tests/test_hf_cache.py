"""Tests for hf_cache module."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from hf_cache import (
    _parse_arch,
    _repo_to_cache_name,
    _find_snapshot,
    scan_model_cache,
    ModelCacheInfo,
    ArchFacts,
    CachedFile,
)


def test_repo_to_cache_name():
    assert _repo_to_cache_name("meta-llama/Llama-3.3-70B-Instruct") == \
        "models--meta-llama--Llama-3.3-70B-Instruct"
    assert _repo_to_cache_name("Qwen/Qwen3-72B") == "models--Qwen--Qwen3-72B"


def test_parse_arch_llama_style(tmp_path):
    cfg = {
        "model_type": "llama",
        "num_hidden_layers": 80,
        "hidden_size": 8192,
        "num_attention_heads": 64,
        "num_key_value_heads": 8,
        "max_position_embeddings": 131072,
        "vocab_size": 128256,
        "intermediate_size": 28672,
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    a = _parse_arch(p)
    assert a is not None
    assert a.num_layers == 80
    assert a.hidden_size == 8192
    assert a.num_heads == 64
    assert a.num_kv_heads == 8
    assert a.context_length == 131072
    assert a.head_dim == 128


def test_parse_arch_gpt2_style(tmp_path):
    cfg = {
        "model_type": "gpt2",
        "n_layer": 12,
        "n_embd": 768,
        "n_head": 12,
        "vocab_size": 50257,
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    a = _parse_arch(p)
    assert a is not None
    assert a.num_layers == 12
    assert a.hidden_size == 768
    assert a.num_heads == 12
    assert a.num_kv_heads == 12  # falls back to num_heads when not specified
    assert a.head_dim == 64


def test_parse_arch_gqa_defaults_to_num_heads(tmp_path):
    cfg = {
        "num_hidden_layers": 32,
        "hidden_size": 4096,
        "num_attention_heads": 32,
        # no num_key_value_heads — should fall back to num_heads
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    a = _parse_arch(p)
    assert a.num_kv_heads == 32


def test_parse_arch_bad_json(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("not valid json {{")
    assert _parse_arch(p) is None


def test_scan_model_cache_not_cached():
    info = scan_model_cache("nonexistent-org/nonexistent-model-xyz-abc")
    assert not info.is_cached
    assert info.arch is None
    assert info.safetensors == []


def test_scan_model_cache_with_fake_snapshot(tmp_path, monkeypatch):
    """Simulate a cached model with config.json and two safetensors shards."""
    import hf_cache

    # Build fake snapshot directory
    snap_dir = tmp_path / "abc123"
    snap_dir.mkdir()

    cfg = {
        "model_type": "llama",
        "num_hidden_layers": 32,
        "hidden_size": 4096,
        "num_attention_heads": 32,
        "num_key_value_heads": 8,
        "max_position_embeddings": 8192,
        "vocab_size": 32000,
    }
    (snap_dir / "config.json").write_text(json.dumps(cfg))
    (snap_dir / "model-00001-of-00002.safetensors").write_bytes(b"x" * 100)
    (snap_dir / "model-00002-of-00002.safetensors").write_bytes(b"y" * 200)
    (snap_dir / "tokenizer.json").write_bytes(b"z" * 50)

    # Patch _find_snapshot so we don't need a real HF cache layout
    monkeypatch.setattr(hf_cache, "_find_snapshot", lambda repo: snap_dir)

    info = scan_model_cache("fake-org/fake-model")

    assert info.is_cached
    assert info.arch is not None
    assert info.arch.num_layers == 32
    assert info.arch.num_kv_heads == 8
    assert len(info.safetensors) == 2
    assert info.total_bytes == 350 + (snap_dir / "config.json").stat().st_size
    assert any(f.name == "tokenizer.json" for f in info.other_files)


def test_cached_file_size_str():
    assert CachedFile("a.safetensors", 4_700_000_000).size_str == "4.7GB"
    assert CachedFile("b.json", 2_100_000).size_str == "2MB"
    assert CachedFile("c.txt", 900).size_str == "1KB"
