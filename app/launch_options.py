#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Use-case presets, LaunchOptions dataclass, CLI arg builder for run.py."""
import json
from dataclasses import dataclass, asdict
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from model_catalog import ModelEntry


@dataclass
class LaunchOptions:
    use_case: str = "chat"

    # vLLM quick settings (None = use spec default / omit the flag)
    max_model_len: Optional[int] = None
    max_num_seqs: Optional[int] = None

    # Tool use (vLLM only)
    tool_use_enabled: bool = False
    tool_call_parser: str = ""          # empty = auto-detect from family
    enable_auto_tool_choice: bool = False
    extra_vllm_args: str = ""           # freeform JSON, merged last, wins

    # General flags
    dev_mode: bool = False
    disable_metal_timeout: bool = True
    disable_trace_capture: bool = False

    # Docker
    docker_image_override: str = ""     # empty = spec default

    # Pass-through run.py flags
    workflow_args: str = ""
    override_tt_config: str = ""        # JSON string
    host_hf_cache: str = ""
    host_volume: str = ""
    host_weights_dir: str = ""
    bind_host: str = ""
    device_id: str = ""
    image_user: str = ""
    skip_system_sw_validation: bool = False


# Use-case names per model type (first entry = default preset)
MODEL_TYPE_USE_CASES: dict = {
    "LLM":       ["chat", "code_completion", "agent_frameworks",
                  "deep_research", "creative_writing", "dev"],
    "VLM":       ["chat", "code_completion", "agent_frameworks",
                  "deep_research", "creative_writing", "dev"],
    "AUDIO":     ["sound_analysis", "dev"],
    "TTS":       ["midi_generation", "music_generation", "dev"],
    "IMAGE":     ["creative", "dev"],
    "VIDEO":     ["creative", "dev"],
    "EMBEDDING": ["semantic_search", "rag_pipeline", "dev"],
    "CNN":       ["standard", "dev"],
}

# One-line descriptions shown as tooltips / inline hints
USE_CASE_DESCRIPTIONS: dict = {
    "chat":             "Balanced — default context (spec max), concurrent requests",
    "code_completion":  "Short context (32K), more concurrent slots for IDE-style completions",
    "agent_frameworks": "Max context (128K), tool calling enabled — for LangChain, AutoGPT, etc.",
    "deep_research":    "Max context (128K), fewer concurrent slots — for long-document analysis",
    "creative_writing": "Long context (64K), few slots — optimized for long-form generation",
    "dev":              "Dev mode, no trace capture — faster startup, lower performance",
    "sound_analysis":   "Default settings for audio analysis tasks",
    "midi_generation":  "Default settings for MIDI generation tasks",
    "music_generation": "Default settings for music generation tasks",
    "creative":         "Default settings for image/video generation tasks",
    "semantic_search":  "Optimized for embedding extraction and nearest-neighbor queries",
    "rag_pipeline":     "Retrieval-augmented generation — balanced context for chunked docs",
    "standard":         "Default settings for classification and inference tasks",
}

# Human-readable labels for use-case chip labels
USE_CASE_LABELS: dict = {
    "chat":             "Chat",
    "code_completion":  "Code completion",
    "agent_frameworks": "Agent frameworks",
    "deep_research":    "Deep research",
    "creative_writing": "Creative writing",
    "dev":              "Dev",
    "sound_analysis":   "Sound analysis",
    "midi_generation":  "MIDI generation",
    "music_generation": "Music generation",
    "creative":         "Creative",
    "semantic_search":  "Semantic search",
    "rag_pipeline":     "RAG pipeline",
    "standard":         "Standard",
}

# Preset → partial LaunchOptions keyword args
PRESETS: dict = {
    "chat": {},
    "code_completion": {
        "max_model_len": 32768,
        "max_num_seqs": 16,
    },
    "agent_frameworks": {
        "max_model_len": 131072,
        "tool_use_enabled": True,
        "enable_auto_tool_choice": True,
    },
    "deep_research": {
        "max_model_len": 131072,
        "max_num_seqs": 4,
    },
    "creative_writing": {
        "max_model_len": 65536,
        "max_num_seqs": 4,
    },
    "dev": {
        "dev_mode": True,
        "disable_metal_timeout": True,
        "disable_trace_capture": True,
    },
    # Non-LLM presets (no vLLM-specific tuning)
    "sound_analysis":   {},
    "midi_generation":  {},
    "music_generation": {},
    "creative":         {},
    "semantic_search":  {},
    "rag_pipeline":     {},
    "standard":         {},
}


_FAMILY_TO_PARSER: dict = {
    "Llama":    "llama3_json",
    "Qwen":     "hermes",
    "Mistral":  "mistral",
    "DeepSeek": "hermes",
    "Gemma":    "hermes",
    "QwQ":      "hermes",
}


def detect_tool_parser(entry: "ModelEntry") -> str:
    """Return the vLLM tool-call parser name for this model's family."""
    return _FAMILY_TO_PARSER.get(entry.family, "hermes")


def apply_preset(use_case: str, entry: "ModelEntry") -> LaunchOptions:
    """Return a fresh LaunchOptions with the preset applied for this use case."""
    kwargs = dict(PRESETS.get(use_case, {}))
    kwargs["use_case"] = use_case
    opts = LaunchOptions(**kwargs)
    # Auto-fill tool parser when tool use is enabled
    if opts.tool_use_enabled and not opts.tool_call_parser:
        opts.tool_call_parser = detect_tool_parser(entry)
    return opts


def build_extra_args(options: LaunchOptions, entry: "ModelEntry") -> List[str]:
    """Return incremental CLI flags to append to the base run.py command.

    The base command (--model, --workflow, --docker-server, --service-port,
    --tt-device, --no-auth) is built by ServerManager.  This function adds
    everything driven by LaunchOptions.  docker_image_override is handled
    separately in LaunchConfig; do not emit --override-docker-image here.

    vLLM JSON merge priority (later wins):
      1. max_model_len / max_num_seqs from options
      2. Tool-use fields when tool_use_enabled
      3. extra_vllm_args JSON (last, wins over everything)
    """
    args: List[str] = []

    # --- vLLM override args (only for vLLM engine) ---
    if entry.inference_engine == "vllm":
        vllm: dict = {}
        if options.max_model_len is not None:
            vllm["max_model_len"] = options.max_model_len
            # max_num_batched_tokens must be >= max_model_len; the model spec
            # sets both to max_context (e.g. 131072).  If we reduce max_model_len
            # without also reducing max_num_batched_tokens, vLLM uses the larger
            # batched-token limit to raise max_model_len back up, defeating the
            # override.  Keep them in sync.
            vllm["max_num_batched_tokens"] = options.max_model_len
        if options.max_num_seqs is not None:
            vllm["max_num_seqs"] = options.max_num_seqs
        if options.tool_use_enabled:
            parser = options.tool_call_parser or detect_tool_parser(entry)
            vllm["tool-call-parser"] = parser
            if options.enable_auto_tool_choice:
                vllm["enable-auto-tool-choice"] = True
        if options.extra_vllm_args:
            try:
                parsed = json.loads(options.extra_vllm_args)
                # Only merge if the parsed value is a dict; a list or scalar
                # would cause dict.update() to raise TypeError.
                if isinstance(parsed, dict):
                    vllm.update(parsed)
            except (json.JSONDecodeError, ValueError):
                pass  # silently skip invalid JSON
        if vllm:
            args += ["--vllm-override-args", json.dumps(vllm)]

    # --- General run.py flags ---
    if options.dev_mode:
        args.append("--dev-mode")
    if options.disable_metal_timeout:
        args.append("--disable-metal-timeout")
    if options.disable_trace_capture:
        args.append("--disable-trace-capture")
    if options.workflow_args:
        args += ["--workflow-args", options.workflow_args]
    if options.override_tt_config:
        args += ["--override-tt-config", options.override_tt_config]
    if options.host_hf_cache:
        args += ["--host-hf-cache", options.host_hf_cache]
    if options.host_volume:
        args += ["--host-volume", options.host_volume]
    if options.host_weights_dir:
        args += ["--host-weights-dir", options.host_weights_dir]
    if options.bind_host:
        args += ["--bind-host", options.bind_host]
    if options.device_id:
        args += ["--device-id", options.device_id]
    if options.image_user:
        args += ["--image-user", options.image_user]
    if options.skip_system_sw_validation:
        args.append("--skip-system-sw-validation")

    return args
