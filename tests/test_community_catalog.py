import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
from community_catalog import parse_community_list, fetch_community_entries

# Real `tt model list --community --json` shape: {"device":..., "scope":...,
# "models": [...]} where each row is a BundleInfo dict (tt-cli's
# modelhub/bundles.py) — name, source, kind, engine, arch (list),
# hardware (list of board/mesh tags), downloads, installed, weights_repo,
# weights_bytes.
JSON_OUTPUT = json.dumps({
    "device": None,
    "scope": "community",
    "models": [
        {"name": "episod/tt-animatediff", "source": "HuggingFace", "kind": "self-contained",
         "engine": None, "arch": ["blackhole"], "hardware": ["p150"],
         "downloads": 12, "installed": False, "weights_repo": "CompVis/stable-diffusion-v1-4",
         "weights_bytes": None},
        {"name": "acme/llama-fast", "source": "HuggingFace", "kind": "thin",
         "engine": "vLLM", "arch": ["wormhole_b0"], "hardware": ["t3000"],
         "downloads": 4, "installed": True, "weights_repo": "meta-llama/Llama-3.2-1B",
         "weights_bytes": 2400000000},
    ],
})

# Fallback text form: the CLI's own table columns are
# (name, source, engine, serving profiles, weights) — used only if --json
# ever fails; parsing is best-effort (first whitespace-separated column is
# the bundle id).
TEXT_OUTPUT = """\
NAME                     SOURCE       ENGINE   SERVING PROFILES   WEIGHTS
episod/tt-animatediff    HuggingFace  -        p150                CompVis/stable-diffusion-v1-4
acme/llama-fast          HuggingFace  vLLM     t3000               meta-llama/Llama-3.2-1B
"""


def test_parse_community_list_json():
    entries = parse_community_list(JSON_OUTPUT)
    assert len(entries) == 2
    assert entries[0].model_id == "episod/tt-animatediff"
    assert entries[0].source == "community"
    assert entries[0].inference_engine == "vllm"  # engine=None defaults to "vllm"
    assert entries[0].device_type == "P150"
    assert entries[0].hf_model_repo == "CompVis/stable-diffusion-v1-4"
    assert entries[1].device_type == "T3K"          # "t3000" -> "T3K" alias
    assert entries[1].inference_engine == "vllm"    # "vLLM" lowercased


def test_parse_community_list_text_fallback():
    entries = parse_community_list(TEXT_OUTPUT)
    assert len(entries) == 2
    assert entries[0].model_id == "episod/tt-animatediff"
    assert entries[1].model_id == "acme/llama-fast"


def test_parse_community_list_unmapped_hardware_is_uppercased():
    """A hardware tag not in the small alias table still gets uppercased,
    matching model_spec.json's own device_type vocabulary (e.g. "p300x2" ->
    "P300X2") — UNKNOWN is reserved for a missing/empty hardware list only."""
    raw = json.dumps({"models": [
        {"name": "x/y", "kind": "thin", "engine": "vLLM", "arch": ["blackhole"],
         "hardware": ["p300x2"], "weights_repo": None},
    ]})
    entries = parse_community_list(raw)
    assert entries[0].device_type == "P300X2"


def test_parse_community_list_missing_hardware_is_unknown():
    raw = json.dumps({"models": [
        {"name": "x/y", "kind": "thin", "engine": None, "arch": [], "hardware": []},
    ]})
    entries = parse_community_list(raw)
    assert entries[0].device_type == "UNKNOWN"


def test_parse_community_list_empty_input():
    assert parse_community_list("") == []
    assert parse_community_list("   \n  ") == []


def test_fetch_community_entries_returns_empty_when_tt_missing():
    with patch("community_catalog.shutil.which", return_value=None):
        assert fetch_community_entries() == []


def test_fetch_community_entries_uses_all_and_json_flags():
    captured = {}
    def _run(cmd, **kwargs):
        captured["cmd"] = cmd
        result = MagicMock()
        result.returncode = 0
        result.stdout = JSON_OUTPUT
        return result

    with patch("community_catalog.shutil.which", return_value="/usr/bin/tt"), \
         patch("community_catalog.subprocess.run", side_effect=_run):
        entries = fetch_community_entries()
    assert captured["cmd"] == ["/usr/bin/tt", "model", "list", "--community", "--all", "--json"]
    assert len(entries) == 2


def test_fetch_community_entries_falls_back_to_text_when_json_flag_unsupported():
    def _run(cmd, **kwargs):
        result = MagicMock()
        if "--json" in cmd:
            result.returncode = 2
            result.stdout = ""
        else:
            result.returncode = 0
            result.stdout = TEXT_OUTPUT
        return result

    with patch("community_catalog.shutil.which", return_value="/usr/bin/tt"), \
         patch("community_catalog.subprocess.run", side_effect=_run):
        entries = fetch_community_entries()
    assert len(entries) == 2
    assert entries[0].model_id == "episod/tt-animatediff"
