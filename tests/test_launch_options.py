# tests/test_launch_options.py
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from launch_options import (
    LaunchOptions, PRESETS, MODEL_TYPE_USE_CASES,
    detect_tool_parser, build_extra_args, apply_preset,
)
from model_catalog import ModelEntry

def _llm_entry(**kw) -> ModelEntry:
    defaults = dict(
        model_id="llama", model_name="meta-llama/Llama-3.3-70B-Instruct",
        display_name="Llama-3.3-70B-Instruct",
        hf_model_repo="meta-llama/Llama-3.3-70B-Instruct",
        model_type="LLM", family="Llama", device_type="P300X2",
        inference_engine="vllm", docker_image="ghcr.io/tenstorrent/vllm:v1",
        status="PRODUCTION", param_count=70.0, min_disk_gb=None, min_ram_gb=None,
    )
    defaults.update(kw)
    return ModelEntry(**defaults)


def test_defaults_produce_no_args():
    # disable_metal_timeout defaults to True, so --disable-metal-timeout is always emitted.
    entry = _llm_entry()
    opts = LaunchOptions()
    assert build_extra_args(opts, entry) == ["--disable-metal-timeout"]


def test_chat_preset_no_vllm_args():
    entry = _llm_entry()
    opts = apply_preset("chat", entry)
    # chat uses spec defaults — no vllm-override-args needed
    args = build_extra_args(opts, entry)
    assert "--vllm-override-args" not in args


def test_code_completion_sets_context():
    entry = _llm_entry()
    opts = apply_preset("code_completion", entry)
    assert opts.max_model_len == 32768
    args = build_extra_args(opts, entry)
    assert "--vllm-override-args" in args
    idx = args.index("--vllm-override-args")
    payload = json.loads(args[idx + 1])
    assert payload["max_model_len"] == 32768


def test_agent_preset_enables_tool_use():
    entry = _llm_entry()
    opts = apply_preset("agent_frameworks", entry)
    assert opts.tool_use_enabled is True
    assert opts.enable_auto_tool_choice is True
    args = build_extra_args(opts, entry)
    idx = args.index("--vllm-override-args")
    payload = json.loads(args[idx + 1])
    assert payload["enable_auto_tool_choice"] is True
    assert payload["tool_call_parser"] == "llama3_json"   # Llama family


def test_dev_preset_sets_flags():
    entry = _llm_entry()
    opts = apply_preset("dev", entry)
    assert opts.dev_mode is True
    assert opts.disable_metal_timeout is True
    assert opts.disable_trace_capture is True
    args = build_extra_args(opts, entry)
    assert "--dev-mode" in args
    assert "--disable-metal-timeout" in args
    assert "--disable-trace-capture" in args


def test_detect_tool_parser_llama():
    assert detect_tool_parser(_llm_entry(family="Llama")) == "llama3_json"

def test_detect_tool_parser_qwen():
    assert detect_tool_parser(_llm_entry(family="Qwen")) == "hermes"

def test_detect_tool_parser_unknown():
    assert detect_tool_parser(_llm_entry(family="Unknown")) == "hermes"


def test_extra_vllm_args_merge_wins():
    entry = _llm_entry()
    opts = LaunchOptions(max_model_len=8192, extra_vllm_args='{"max_model_len": 4096}')
    args = build_extra_args(opts, entry)
    payload = json.loads(args[args.index("--vllm-override-args") + 1])
    # extra_vllm_args wins
    assert payload["max_model_len"] == 4096


def test_non_vllm_engine_no_vllm_args():
    entry = _llm_entry(inference_engine="media")
    opts = LaunchOptions(max_model_len=8192)
    args = build_extra_args(opts, entry)
    assert "--vllm-override-args" not in args


def test_advanced_flags_pass_through():
    entry = _llm_entry()
    opts = LaunchOptions(
        host_hf_cache="~/.cache/hf",
        host_volume="/data/vol",
        bind_host="127.0.0.1",
        device_id="0",
        image_user="1001",
        skip_system_sw_validation=True,
    )
    args = build_extra_args(opts, entry)
    assert "--host-hf-cache" in args
    assert "--host-volume" in args
    assert "--bind-host" in args
    assert "--device-id" in args
    assert "--image-user" in args
    assert "--skip-system-sw-validation" in args
    # Verify the values that follow each flag are correct
    assert args[args.index("--host-hf-cache") + 1] == "~/.cache/hf"
    assert args[args.index("--host-volume") + 1] == "/data/vol"
    assert args[args.index("--device-id") + 1] == "0"


def test_workflow_and_config_flags():
    """workflow_args, override_tt_config, and host_weights_dir should produce
    their corresponding CLI flags with the correct values."""
    entry = _llm_entry()
    opts = LaunchOptions(
        workflow_args="param=value",
        override_tt_config='{"trace_region_size": 4096}',
        host_weights_dir="/weights",
    )
    args = build_extra_args(opts, entry)
    assert "--workflow-args" in args
    assert args[args.index("--workflow-args") + 1] == "param=value"
    assert "--override-tt-config" in args
    assert args[args.index("--override-tt-config") + 1] == '{"trace_region_size": 4096}'
    assert "--host-weights-dir" in args
    assert args[args.index("--host-weights-dir") + 1] == "/weights"


def test_dev_preset_non_vllm_no_vllm_args():
    """Dev preset on a non-vLLM engine should emit dev flags but no
    --vllm-override-args (since the engine does not use vLLM)."""
    entry = _llm_entry(inference_engine="forge")
    opts = apply_preset("dev", entry)
    args = build_extra_args(opts, entry)
    assert "--dev-mode" in args
    assert "--disable-metal-timeout" in args
    assert "--vllm-override-args" not in args


def test_invalid_extra_vllm_json_ignored():
    entry = _llm_entry()
    opts = LaunchOptions(extra_vllm_args="not-valid-json")
    # Should not raise; invalid JSON is silently skipped
    args = build_extra_args(opts, entry)
    # No vllm-override-args since only extra_vllm_args was set (and invalid)
    assert "--vllm-override-args" not in args


def test_model_type_use_cases_coverage():
    for mt in ("LLM", "VLM", "IMAGE", "VIDEO", "AUDIO", "EMBEDDING", "TTS", "CNN"):
        assert mt in MODEL_TYPE_USE_CASES, f"Missing use cases for {mt}"
        assert len(MODEL_TYPE_USE_CASES[mt]) >= 1
