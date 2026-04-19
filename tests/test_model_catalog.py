import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
from model_catalog import ModelCatalog, extract_family

MINI_SPEC = {
    "schema_version": "2",
    "model_specs": {
        "id_llama_70b": {
            "T3K": {
                "vllm": {
                    "impl": {
                        "model_id": "id_llama_70b",
                        "hf_model_repo": "meta-llama/Llama-3.3-70B-Instruct",
                        "model_type": "LLM",
                        "docker_image": "ghcr.io/tt/vllm:latest",
                        "status": "COMPLETE",
                        "param_count": 70.0,
                        "min_disk_gb": 140.0,
                        "min_ram_gb": 16.0,
                    }
                }
            }
        },
        "id_llama_1b": {
            "N150": {
                "vllm": {
                    "impl": {
                        "model_id": "id_llama_1b",
                        "hf_model_repo": "meta-llama/Llama-3.2-1B",
                        "model_type": "LLM",
                        "docker_image": "ghcr.io/tt/vllm:latest",
                        "status": "FUNCTIONAL",
                        "param_count": 1.0,
                        "min_disk_gb": 3.0,
                        "min_ram_gb": 8.0,
                    }
                }
            }
        },
        "id_wan_t2v": {
            "P300X2": {
                "media": {
                    "impl": {
                        "model_id": "id_wan_t2v",
                        "hf_model_repo": "Wan-AI/Wan2.2-T2V-A14B-Diffusers",
                        "model_type": "VIDEO",
                        "docker_image": "ghcr.io/tt/media:latest",
                        "status": "FUNCTIONAL",
                        "param_count": 14.0,
                        "min_disk_gb": 38.0,
                        "min_ram_gb": 64.0,
                    }
                }
            }
        },
    },
}


def test_load_and_tree(tmp_path):
    spec = tmp_path / "model_spec.json"
    spec.write_text(json.dumps(MINI_SPEC))
    cat = ModelCatalog.load(spec)
    tree = cat.get_tree()
    assert "LLM" in tree
    assert "Llama" in tree["LLM"]
    assert len(tree["LLM"]["Llama"]) == 2
    assert "VIDEO" in tree


def test_get_entry(tmp_path):
    spec = tmp_path / "model_spec.json"
    spec.write_text(json.dumps(MINI_SPEC))
    cat = ModelCatalog.load(spec)
    entry = cat.get_entry("id_llama_70b", "T3K")
    assert entry is not None
    assert entry.inference_engine == "vllm"
    assert entry.param_count == 70.0


def test_compatible_filter(tmp_path):
    spec = tmp_path / "model_spec.json"
    spec.write_text(json.dumps(MINI_SPEC))
    cat = ModelCatalog.load(spec)
    filtered = cat.get_compatible(["N150"])
    tree = filtered.get_tree()
    assert len(tree["LLM"]["Llama"]) == 1
    assert tree["LLM"]["Llama"][0].device_type == "N150"


def test_extract_family():
    assert extract_family("Llama-3.3-70B-Instruct") == "Llama"
    assert extract_family("Qwen3-8B") == "Qwen"
    assert extract_family("DeepSeek-R1-Distill-Llama-70B") == "DeepSeek"
    assert extract_family("Wan2.2-T2V-A14B-Diffusers") == "Wan"
    assert extract_family("meta-llama/Llama-3.2-1B") == "Llama"
    assert extract_family("mistralai/Mistral-7B-v0.3") == "Mistral"
