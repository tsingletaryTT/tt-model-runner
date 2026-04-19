# Launch Configuration Panel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a full-screen configuration panel that appears after model selection, letting users pick a use-case preset, tune every launch-time setting, select a local Docker image, and save named profiles.

**Architecture:** After model selection, the right panel switches from the log view to a `ConfigPanel` widget via `Gtk.Stack`. On launch, it switches back to logs. `ConfigPanel` builds a `LaunchOptions` object that `server_manager.launch()` converts to run.py CLI flags via `build_extra_args()`. Profiles are JSON files under `~/.config/tt-runner-gui/profiles/`.

**Tech Stack:** GTK 4 (gi.repository), Python 3.12, GLib.idle_add threading pattern, subprocess for docker images scan.

**Already done (do not re-implement):**
- `app/docker_images.py` — `scan_local_images()` exists but has no tests yet
- `app/ghcr_resolver.py` — GHCR tag resolution
- `app/server_manager.py` — `LaunchConfig.docker_image_override` field exists

---

## File map

| File | Action | Responsibility |
|------|--------|---------------|
| `app/launch_options.py` | **Create** | `LaunchOptions` dataclass, use-case presets, `detect_tool_parser`, `build_extra_args` |
| `app/profiles.py` | **Create** | `save_profile`, `load_profile`, `list_profiles`, `delete_profile` |
| `app/config_panel.py` | **Create** | `ConfigPanel(Gtk.Box)` — full configuration UI widget |
| `app/server_manager.py` | **Modify** | Add `options` + `inference_engine` fields to `LaunchConfig`; call `build_extra_args` in `launch()` |
| `app/main_window.py` | **Modify** | `MainPanel` gets `Gtk.Stack`; `MainWindow` switches pages on model-select / launch / idle |
| `tests/test_launch_options.py` | **Create** | TDD tests for pure logic |
| `tests/test_profiles.py` | **Create** | TDD tests for profile persistence |
| `tests/test_docker_images.py` | **Create** | Tests for existing `docker_images.py` |

---

## Task 1: `app/launch_options.py` — data model + CLI builder

**Files:**
- Create: `app/launch_options.py`
- Test: `tests/test_launch_options.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_launch_options.py
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
    entry = _llm_entry()
    opts = LaunchOptions()
    assert build_extra_args(opts, entry) == []


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
    import json
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
    import json
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
    import json
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/ttuser/code/tt-model-runner-gui
python -m pytest tests/test_launch_options.py -v 2>&1 | head -20
```

Expected: ImportError — `launch_options` does not exist yet.

- [ ] **Step 3: Implement `app/launch_options.py`**

```python
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
    disable_metal_timeout: bool = False
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
        if options.max_num_seqs is not None:
            vllm["max_num_seqs"] = options.max_num_seqs
        if options.tool_use_enabled:
            parser = options.tool_call_parser or detect_tool_parser(entry)
            vllm["tool_call_parser"] = parser
            if options.enable_auto_tool_choice:
                vllm["enable_auto_tool_choice"] = True
        if options.extra_vllm_args:
            try:
                vllm.update(json.loads(options.extra_vllm_args))
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_launch_options.py -v
```

Expected: all 12 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/launch_options.py tests/test_launch_options.py
git commit -m "feat: add launch_options — use-case presets, LaunchOptions dataclass, CLI arg builder"
```

---

## Task 2: `app/profiles.py` — profile persistence

**Files:**
- Create: `app/profiles.py`
- Test: `tests/test_profiles.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_profiles.py
import json
import sys
from pathlib import Path
import tempfile
import pytest
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from launch_options import LaunchOptions
import profiles as prof_module


@pytest.fixture(autouse=True)
def tmp_profiles_dir(tmp_path, monkeypatch):
    """Redirect all profile I/O to a temp directory."""
    monkeypatch.setattr(prof_module, "_PROFILES_DIR", tmp_path / "profiles")
    return tmp_path / "profiles"


def test_save_and_load_roundtrip():
    opts = LaunchOptions(use_case="dev", dev_mode=True, max_model_len=32768)
    prof_module.save_profile("my-profile", "test profile", "LLM", opts)
    loaded = prof_module.load_profile("my-profile")
    assert loaded is not None
    assert loaded["name"] == "my-profile"
    assert loaded["options"]["dev_mode"] is True
    assert loaded["options"]["max_model_len"] == 32768


def test_load_nonexistent_returns_none():
    assert prof_module.load_profile("does-not-exist") is None


def test_list_profiles_empty():
    assert prof_module.list_profiles() == []


def test_list_profiles_returns_saved():
    prof_module.save_profile("p1", "", "LLM", LaunchOptions())
    prof_module.save_profile("p2", "", "IMAGE", LaunchOptions())
    result = prof_module.list_profiles()
    assert len(result) == 2
    names = {p["name"] for p in result}
    assert names == {"p1", "p2"}


def test_list_profiles_filter_by_model_type():
    prof_module.save_profile("llm-prof", "", "LLM", LaunchOptions())
    prof_module.save_profile("img-prof", "", "IMAGE", LaunchOptions())
    result = prof_module.list_profiles(model_type="LLM")
    assert len(result) == 1
    assert result[0]["name"] == "llm-prof"


def test_delete_profile():
    prof_module.save_profile("to-delete", "", "LLM", LaunchOptions())
    assert prof_module.delete_profile("to-delete") is True
    assert prof_module.load_profile("to-delete") is None


def test_delete_nonexistent_returns_false():
    assert prof_module.delete_profile("ghost") is False


def test_corrupt_profile_skipped_in_list(tmp_profiles_dir):
    tmp_profiles_dir.mkdir(parents=True, exist_ok=True)
    (tmp_profiles_dir / "bad.json").write_text("not json")
    prof_module.save_profile("good", "", "LLM", LaunchOptions())
    result = prof_module.list_profiles()
    # corrupt file is silently skipped
    assert len(result) == 1
    assert result[0]["name"] == "good"


def test_overwrite_existing_profile():
    prof_module.save_profile("p", "", "LLM", LaunchOptions(dev_mode=False))
    prof_module.save_profile("p", "", "LLM", LaunchOptions(dev_mode=True))
    loaded = prof_module.load_profile("p")
    assert loaded["options"]["dev_mode"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_profiles.py -v 2>&1 | head -10
```

Expected: ImportError — `profiles` module not found.

- [ ] **Step 3: Implement `app/profiles.py`**

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Profile persistence — save/load/list/delete named LaunchOptions profiles.

Profiles are stored as JSON files under ~/.config/tt-runner-gui/profiles/.
Each profile file: { name, description, model_type, created, options: {...} }.
"""
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from launch_options import LaunchOptions

_PROFILES_DIR = Path.home() / ".config" / "tt-runner-gui" / "profiles"


def save_profile(
    name: str,
    description: str,
    model_type: str,
    options: LaunchOptions,
) -> None:
    """Write profile to disk, overwriting if it already exists."""
    _PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "name": name,
        "description": description,
        "model_type": model_type,
        "created": datetime.now().isoformat(timespec="seconds"),
        "options": asdict(options),
    }
    (_PROFILES_DIR / f"{name}.json").write_text(json.dumps(data, indent=2))


def load_profile(name: str) -> Optional[dict]:
    """Return profile dict or None if not found / corrupt."""
    path = _PROFILES_DIR / f"{name}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def list_profiles(model_type: str = "") -> List[dict]:
    """Return all profiles, optionally filtered by model_type.

    Corrupt files are silently skipped.  Profiles with no model_type match any
    filter.
    """
    if not _PROFILES_DIR.exists():
        return []
    result = []
    for p in sorted(_PROFILES_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if model_type and data.get("model_type") and data["model_type"] != model_type:
            continue
        result.append(data)
    return result


def delete_profile(name: str) -> bool:
    """Delete profile file.  Returns True if deleted, False if not found."""
    path = _PROFILES_DIR / f"{name}.json"
    if not path.exists():
        return False
    path.unlink()
    return True


def profile_to_options(profile: dict) -> LaunchOptions:
    """Reconstruct a LaunchOptions from a loaded profile dict."""
    opts_data = profile.get("options", {})
    known = {f.name for f in LaunchOptions.__dataclass_fields__.values()}
    filtered = {k: v for k, v in opts_data.items() if k in known}
    return LaunchOptions(**filtered)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_profiles.py -v
```

Expected: all 9 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/profiles.py tests/test_profiles.py
git commit -m "feat: add profiles — save/load/delete named LaunchOptions profiles"
```

---

## Task 3: `tests/test_docker_images.py` — test existing scanner

`app/docker_images.py` was already implemented. Write tests to verify it.

**Files:**
- Test: `tests/test_docker_images.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_docker_images.py
import sys
from pathlib import Path
import subprocess
import pytest
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from docker_images import scan_local_images, DockerImage

_FAKE_OUTPUT = """\
ghcr.io/tenstorrent/tt-inference-server/vllm-tt-metal:v0.56.0\t4.21 GB\t3 days ago
ghcr.io/tenstorrent/tt-media-inference-server:0.12.0-2508220\t2.10 GB\t5 hours ago
ubuntu:22.04\t77.8 MB\t2 weeks ago
"""


def _mock_run(cmd, **kwargs):
    class R:
        returncode = 0
        stdout = _FAKE_OUTPUT
        stderr = ""
    return R()


def test_filters_tt_images_only(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _mock_run)
    images = scan_local_images()
    assert len(images) == 2
    assert all(img.is_tt for img in images)
    repos = {img.repo_tag for img in images}
    assert "ubuntu:22.04" not in str(repos)


def test_fields_populated(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _mock_run)
    images = scan_local_images()
    media = next(i for i in images if "tt-media" in i.repo_tag)
    assert media.size_str == "2.10 GB"
    assert media.created_str == "5 hours ago"


def test_spec_default_not_pulled_prepended(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _mock_run)
    spec_image = "ghcr.io/tenstorrent/tt-media-inference-server:0.12.0-NEW"
    images = scan_local_images(spec_default=spec_image)
    assert images[0].repo_tag == spec_image
    assert images[0].size_str == "—"
    assert images[0].created_str == "not pulled"


def test_spec_default_found_moved_to_front(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _mock_run)
    spec_image = "ghcr.io/tenstorrent/tt-media-inference-server:0.12.0-2508220"
    images = scan_local_images(spec_default=spec_image)
    assert images[0].repo_tag == spec_image
    assert images[0].size_str == "2.10 GB"


def test_docker_not_available(monkeypatch):
    def _raise(*a, **kw):
        raise FileNotFoundError("docker not found")
    monkeypatch.setattr(subprocess, "run", _raise)
    assert scan_local_images() == []


def test_docker_error_returns_empty(monkeypatch):
    class R:
        returncode = 1
        stdout = ""
        stderr = "permission denied"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: R())
    assert scan_local_images() == []


def test_short_tag_property():
    img = DockerImage(
        repo_tag="ghcr.io/tenstorrent/vllm-tt-metal:v0.56.0",
        size_str="4 GB",
        created_str="1 day ago",
        is_tt=True,
    )
    assert img.short_tag == "vllm-tt-metal:v0.56.0"
```

- [ ] **Step 2: Run tests**

```bash
python -m pytest tests/test_docker_images.py -v
```

Expected: all 7 tests PASS (docker_images.py already exists).

- [ ] **Step 3: Commit**

```bash
git add tests/test_docker_images.py
git commit -m "test: add coverage for docker_images.scan_local_images"
```

---

## Task 4: `app/server_manager.py` — wire LaunchOptions into launch

Add `options` and `inference_engine` to `LaunchConfig`; call `build_extra_args` in `launch()`.

**Files:**
- Modify: `app/server_manager.py`

- [ ] **Step 1: Add fields to `LaunchConfig`**

In `app/server_manager.py`, find the `LaunchConfig` dataclass (around line 108) and add two fields at the end:

```python
@dataclass
class LaunchConfig:
    repo_path: Path
    model_name: str
    device: str
    port: str = "8000"
    hf_token: Optional[str] = None
    no_auth: bool = True
    docker_image_override: str = ""
    options: Optional["LaunchOptions"] = None   # ← add this
    inference_engine: str = ""                  # ← add this (needed by build_extra_args)
```

Add the import at the top of the file (alongside existing imports):

```python
from typing import Callable, List, Optional, TYPE_CHECKING
if TYPE_CHECKING:
    from launch_options import LaunchOptions
```

- [ ] **Step 2: Use `options` in `launch()`**

In `launch()`, after the existing `if config.docker_image_override:` block, add:

```python
        # Resolve docker_image_override from options when options is provided
        docker_override = config.docker_image_override
        if config.options and config.options.docker_image_override:
            docker_override = config.options.docker_image_override
        if docker_override:
            cmd += ["--override-docker-image", docker_override]

        # Append extra launch options flags
        if config.options and config.inference_engine:
            from launch_options import build_extra_args
            # Temporarily set inference_engine on a dummy entry-like object
            class _E:
                inference_engine = config.inference_engine
                family = ""
            cmd += build_extra_args(config.options, _E())
```

Remove the existing `if config.docker_image_override:` block (it's superseded by the code above).

Full replacement for the docker/options section in `launch()` — find these lines:

```python
        if config.docker_image_override:
            cmd += ["--override-docker-image", config.docker_image_override]
```

Replace with:

```python
        # docker_image_override: options wins over direct config field
        _docker_img = (
            (config.options.docker_image_override if config.options else "")
            or config.docker_image_override
        )
        if _docker_img:
            cmd += ["--override-docker-image", _docker_img]

        if config.options:
            from launch_options import build_extra_args

            class _EntryProxy:
                inference_engine = config.inference_engine or ""
                family = ""

            cmd += build_extra_args(config.options, _EntryProxy())
```

- [ ] **Step 3: Run full test suite**

```bash
python -m pytest tests/ -v 2>&1 | tail -15
```

Expected: all tests PASS (no regressions).

- [ ] **Step 4: Commit**

```bash
git add app/server_manager.py
git commit -m "feat: wire LaunchOptions into server_manager.launch via build_extra_args"
```

---

## Task 5: `app/config_panel.py` — ConfigPanel GTK widget

No automated GTK tests for this task (per design spec). The widget is tested by running the app.

**Files:**
- Create: `app/config_panel.py`

- [ ] **Step 1: Implement `ConfigPanel`**

Create `app/config_panel.py` with the full widget. This is a `Gtk.Box` (vertical) that holds:
- Model strip (name, type, engine, device, status)
- Profile bar (dropdown + Save + Delete)
- USE CASE chips
- QUICK SETTINGS (vLLM section + general section)
- Docker image picker
- Advanced expander
- Command preview

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""ConfigPanel — full-screen launch configuration widget.

Replaces the log view when idle.  Shows use-case presets, quick settings,
docker image picker, advanced fields, and a live command preview.
"""
import json
import threading
from typing import Callable, List, Optional

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

from docker_images import DockerImage, scan_local_images
from launch_options import (
    LaunchOptions, MODEL_TYPE_USE_CASES, USE_CASE_LABELS,
    apply_preset, build_extra_args, detect_tool_parser,
)
from model_catalog import ModelEntry
from profiles import (
    delete_profile, list_profiles, profile_to_options, save_profile,
)
from worker import idle_add_once


# Known vLLM tool-call parsers (shown in parser dropdown)
_PARSERS = ["hermes", "llama3_json", "mistral", "pythonic"]

# Context length preset values for dropdown
_CTX_OPTIONS = [8192, 16384, 32768, 65536, 131072]
# Max concurrent seqs preset values
_SEQ_OPTIONS = [1, 4, 8, 16, 32, 64]


class ConfigPanel(Gtk.Box):
    """Full-screen configuration panel shown when a model is selected and idle."""

    def __init__(self, on_options_changed: Callable[[LaunchOptions], None]):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._on_options_changed = on_options_changed
        self._entry: Optional[ModelEntry] = None
        self._options = LaunchOptions()
        self._use_case_btns: dict = {}        # key → Gtk.ToggleButton
        self._docker_images: List[DockerImage] = []
        self._preview_source: Optional[int] = None
        self._inhibit_signals: bool = False   # suppress change callbacks while updating UI
        self._build()

    # ------------------------------------------------------------------ build

    def _build(self):
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        inner.set_margin_start(12)
        inner.set_margin_end(12)
        inner.set_margin_top(8)
        inner.set_margin_bottom(8)

        # --- Model strip ---
        self._model_strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._model_strip.set_margin_bottom(6)
        self._strip_name = Gtk.Label(label="")
        self._strip_name.set_markup("<b>Select a model</b>")
        self._strip_name.set_halign(Gtk.Align.START)
        self._strip_name.set_hexpand(True)
        self._model_strip.append(self._strip_name)
        self._strip_badge = Gtk.Label(label="")
        self._strip_badge.add_css_class("pill")
        self._strip_badge.add_css_class("pill-idle")
        self._model_strip.append(self._strip_badge)
        inner.append(self._model_strip)
        inner.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # --- Profile bar ---
        profile_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        profile_bar.set_margin_top(6)
        profile_bar.set_margin_bottom(6)
        lbl = Gtk.Label(label="Profile:")
        lbl.add_css_class("muted")
        profile_bar.append(lbl)
        self._profile_combo = Gtk.ComboBoxText()
        self._profile_combo.append("__none__", "No profile")
        self._profile_combo.set_active(0)
        self._profile_combo.set_hexpand(True)
        self._profile_combo.connect("changed", self._on_profile_selected)
        profile_bar.append(self._profile_combo)
        self._save_profile_btn = Gtk.Button(label="Save…")
        self._save_profile_btn.connect("clicked", self._on_save_profile)
        profile_bar.append(self._save_profile_btn)
        self._del_profile_btn = Gtk.Button(label="✕")
        self._del_profile_btn.add_css_class("destructive-action")
        self._del_profile_btn.connect("clicked", self._on_delete_profile)
        self._del_profile_btn.set_sensitive(False)
        profile_bar.append(self._del_profile_btn)
        inner.append(profile_bar)
        inner.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # --- USE CASE chips ---
        uc_lbl = Gtk.Label(label="USE CASE")
        uc_lbl.add_css_class("muted")
        uc_lbl.set_halign(Gtk.Align.START)
        uc_lbl.set_margin_top(8)
        uc_lbl.set_margin_bottom(4)
        inner.append(uc_lbl)
        self._uc_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._uc_box.set_margin_bottom(8)
        inner.append(self._uc_box)

        # --- QUICK SETTINGS ---
        qs_lbl = Gtk.Label(label="QUICK SETTINGS")
        qs_lbl.add_css_class("muted")
        qs_lbl.set_halign(Gtk.Align.START)
        qs_lbl.set_margin_bottom(4)
        inner.append(qs_lbl)

        # vLLM-only row (context + seqs + tool use)
        self._vllm_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._vllm_box.set_margin_bottom(6)

        ctx_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ctx_lbl = Gtk.Label(label="Context length")
        ctx_lbl.add_css_class("muted")
        ctx_lbl.set_hexpand(True)
        ctx_lbl.set_halign(Gtk.Align.START)
        ctx_row.append(ctx_lbl)
        self._ctx_combo = Gtk.ComboBoxText.new_with_entry()
        for v in _CTX_OPTIONS:
            self._ctx_combo.append_text(str(v))
        self._ctx_combo.append_text("default")
        self._ctx_combo.set_active(_CTX_OPTIONS.index(131072))
        self._ctx_combo.get_child().connect("changed", self._on_ctx_changed)
        ctx_row.append(self._ctx_combo)
        seq_lbl = Gtk.Label(label="Max concurrent")
        seq_lbl.add_css_class("muted")
        seq_lbl.set_hexpand(True)
        seq_lbl.set_halign(Gtk.Align.START)
        ctx_row.append(seq_lbl)
        self._seq_combo = Gtk.ComboBoxText.new_with_entry()
        for v in _SEQ_OPTIONS:
            self._seq_combo.append_text(str(v))
        self._seq_combo.append_text("default")
        self._seq_combo.set_active(len(_SEQ_OPTIONS))   # default
        self._seq_combo.get_child().connect("changed", self._on_seq_changed)
        ctx_row.append(self._seq_combo)
        self._vllm_box.append(ctx_row)

        # Tool use row
        tool_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._tool_toggle = Gtk.ToggleButton(label="🔧 Tool use")
        self._tool_toggle.connect("toggled", self._on_tool_toggled)
        tool_row.append(self._tool_toggle)
        parser_lbl = Gtk.Label(label="Parser:")
        parser_lbl.add_css_class("muted")
        tool_row.append(parser_lbl)
        self._parser_combo = Gtk.ComboBoxText()
        for p in _PARSERS:
            self._parser_combo.append_text(p)
        self._parser_combo.set_active(0)
        self._parser_combo.connect("changed", self._on_parser_changed)
        tool_row.append(self._parser_combo)
        self._auto_tool_check = Gtk.CheckButton(label="Auto tool choice")
        self._auto_tool_check.connect("toggled", self._on_any_change)
        tool_row.append(self._auto_tool_check)
        self._tool_detail_row = tool_row   # shown/hidden with tool use
        self._vllm_box.append(tool_row)

        inner.append(self._vllm_box)

        # General row (dev mode + timeout + workflow args) — shown for all engines
        gen_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        gen_row.set_margin_bottom(6)
        self._dev_mode_check = Gtk.CheckButton(label="Dev mode")
        self._dev_mode_check.connect("toggled", self._on_any_change)
        gen_row.append(self._dev_mode_check)
        self._no_timeout_check = Gtk.CheckButton(label="Disable TT timeout")
        self._no_timeout_check.connect("toggled", self._on_any_change)
        gen_row.append(self._no_timeout_check)
        wf_lbl = Gtk.Label(label="Workflow args:")
        wf_lbl.add_css_class("muted")
        gen_row.append(wf_lbl)
        self._workflow_entry = Gtk.Entry()
        self._workflow_entry.set_placeholder_text("param=value …")
        self._workflow_entry.set_hexpand(True)
        self._workflow_entry.connect("changed", self._on_any_change)
        gen_row.append(self._workflow_entry)
        inner.append(gen_row)
        inner.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # --- Docker image picker ---
        docker_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        docker_row.set_margin_top(6)
        docker_row.set_margin_bottom(6)
        docker_lbl = Gtk.Label(label="Docker image")
        docker_lbl.add_css_class("muted")
        docker_row.append(docker_lbl)
        self._docker_combo = Gtk.ComboBoxText()
        self._docker_combo.append("__spec__", "spec default")
        self._docker_combo.set_active(0)
        self._docker_combo.set_hexpand(True)
        self._docker_combo.connect("changed", self._on_docker_changed)
        docker_row.append(self._docker_combo)
        refresh_btn = Gtk.Button(label="⟳")
        refresh_btn.connect("clicked", self._on_docker_refresh)
        docker_row.append(refresh_btn)
        self._docker_status = Gtk.Label(label="")
        self._docker_status.add_css_class("muted")
        docker_row.append(self._docker_status)
        inner.append(docker_row)

        # --- Advanced expander ---
        exp = Gtk.Expander(label="Advanced")
        exp.set_margin_top(4)
        exp.set_margin_bottom(4)
        adv_grid = Gtk.Grid()
        adv_grid.set_column_spacing(8)
        adv_grid.set_row_spacing(4)
        adv_grid.set_margin_top(6)
        adv_grid.set_margin_start(8)

        def _adv_row(grid, row, label, widget):
            lbl = Gtk.Label(label=label)
            lbl.add_css_class("muted")
            lbl.set_halign(Gtk.Align.END)
            grid.attach(lbl, 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)

        self._vllm_extra_entry = Gtk.Entry()
        self._vllm_extra_entry.set_placeholder_text('{"block_size": 64}')
        self._vllm_extra_entry.set_hexpand(True)
        self._vllm_extra_entry.connect("changed", self._on_vllm_extra_changed)
        _adv_row(adv_grid, 0, "vLLM args (JSON)", self._vllm_extra_entry)

        self._tt_config_entry = Gtk.Entry()
        self._tt_config_entry.set_placeholder_text('{"trace_region_size": 4096}')
        self._tt_config_entry.set_hexpand(True)
        self._tt_config_entry.connect("changed", self._on_any_change)
        _adv_row(adv_grid, 1, "TT config (JSON)", self._tt_config_entry)

        self._hf_cache_entry = Gtk.Entry()
        self._hf_cache_entry.set_placeholder_text("~/.cache/huggingface")
        self._hf_cache_entry.set_hexpand(True)
        self._hf_cache_entry.connect("changed", self._on_any_change)
        _adv_row(adv_grid, 2, "Host HF cache", self._hf_cache_entry)

        self._volume_entry = Gtk.Entry()
        self._volume_entry.set_hexpand(True)
        self._volume_entry.connect("changed", self._on_any_change)
        _adv_row(adv_grid, 3, "Host volume", self._volume_entry)

        self._weights_entry = Gtk.Entry()
        self._weights_entry.set_hexpand(True)
        self._weights_entry.connect("changed", self._on_any_change)
        _adv_row(adv_grid, 4, "Weights dir", self._weights_entry)

        self._bind_entry = Gtk.Entry()
        self._bind_entry.set_placeholder_text("0.0.0.0")
        self._bind_entry.set_hexpand(True)
        self._bind_entry.connect("changed", self._on_any_change)
        _adv_row(adv_grid, 5, "Bind host", self._bind_entry)

        self._device_id_entry = Gtk.Entry()
        self._device_id_entry.set_placeholder_text("0")
        self._device_id_entry.set_hexpand(True)
        self._device_id_entry.connect("changed", self._on_any_change)
        _adv_row(adv_grid, 6, "Device ID", self._device_id_entry)

        self._image_user_entry = Gtk.Entry()
        self._image_user_entry.set_placeholder_text("1000")
        self._image_user_entry.set_hexpand(True)
        self._image_user_entry.connect("changed", self._on_any_change)
        _adv_row(adv_grid, 7, "Image user (UID)", self._image_user_entry)

        self._skip_sw_check = Gtk.CheckButton(label="Skip SW validation")
        self._skip_sw_check.connect("toggled", self._on_any_change)
        adv_grid.attach(self._skip_sw_check, 0, 8, 2, 1)

        self._no_trace_check = Gtk.CheckButton(label="Disable trace capture")
        self._no_trace_check.connect("toggled", self._on_any_change)
        adv_grid.attach(self._no_trace_check, 0, 9, 2, 1)

        exp.set_child(adv_grid)
        inner.append(exp)
        inner.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # --- Command preview ---
        prev_lbl = Gtk.Label(label="COMMAND PREVIEW")
        prev_lbl.add_css_class("muted")
        prev_lbl.set_halign(Gtk.Align.START)
        prev_lbl.set_margin_top(6)
        inner.append(prev_lbl)
        self._preview_buf = Gtk.TextBuffer()
        self._preview_view = Gtk.TextView(buffer=self._preview_buf)
        self._preview_view.set_editable(False)
        self._preview_view.set_cursor_visible(False)
        self._preview_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._preview_view.set_monospace(True)
        self._preview_view.add_css_class("log-view")
        self._preview_view.set_size_request(-1, 60)
        inner.append(self._preview_view)

        scroll.set_child(inner)
        self.append(scroll)

    # --------------------------------------------------------------- model set

    def set_model(self, entry: ModelEntry) -> None:
        """Update the panel for a newly selected model entry."""
        self._entry = entry
        self._inhibit_signals = True

        # Model strip
        self._strip_name.set_markup(
            f"<b>{entry.display_name}</b>"
            f"  <span foreground='#607D8B'>{entry.model_type} · {entry.inference_engine} · {entry.device_type}</span>"
        )
        badge_text = entry.status.upper()
        badge_css = {
            "PRODUCTION": "pill-ready",
            "EXPERIMENTAL": "pill-loading",
        }.get(badge_text, "pill-idle")
        self._strip_badge.set_text(badge_text)
        self._strip_badge.set_css_classes(["pill", badge_css])

        # Rebuild use-case chips
        self._populate_use_cases(entry)

        # Show/hide vLLM section
        self._vllm_box.set_visible(entry.inference_engine == "vllm")

        # Apply default preset
        default_uc = MODEL_TYPE_USE_CASES.get(entry.model_type, ["dev"])[0]
        self._apply_use_case(default_uc)

        # Reload profiles for this model type
        self._refresh_profiles(entry.model_type)

        self._inhibit_signals = False

        # Refresh docker images in background
        self._refresh_docker_images(entry.docker_image)
        self._update_preview()

    # ------------------------------------------------------------- use cases

    def _populate_use_cases(self, entry: ModelEntry) -> None:
        for child in list(self._uc_box):
            self._uc_box.remove(child)
        self._use_case_btns.clear()
        use_cases = MODEL_TYPE_USE_CASES.get(entry.model_type, ["dev"])
        for uc in use_cases:
            btn = Gtk.ToggleButton(label=USE_CASE_LABELS.get(uc, uc))
            btn.add_css_class("chip")
            btn.connect("toggled", self._on_uc_toggled, uc)
            self._uc_box.append(btn)
            self._use_case_btns[uc] = btn

    def _apply_use_case(self, use_case: str) -> None:
        """Set active chip and fill quick-settings fields from preset."""
        if self._entry is None:
            return
        self._inhibit_signals = True
        self._options = apply_preset(use_case, self._entry)

        # Highlight selected chip
        for uc, btn in self._use_case_btns.items():
            btn.handler_block_by_func(self._on_uc_toggled)
            btn.set_active(uc == use_case)
            btn.handler_unblock_by_func(self._on_uc_toggled)

        # Fill quick-settings widgets from options
        self._sync_widgets_to_options()
        self._inhibit_signals = False

    def _sync_widgets_to_options(self) -> None:
        """Push current self._options values into all widgets (no callbacks fired)."""
        # Context length
        ctx_entry = self._ctx_combo.get_child()
        if self._options.max_model_len is not None:
            ctx_entry.set_text(str(self._options.max_model_len))
        else:
            ctx_entry.set_text("default")

        # Max concurrent seqs
        seq_entry = self._seq_combo.get_child()
        if self._options.max_num_seqs is not None:
            seq_entry.set_text(str(self._options.max_num_seqs))
        else:
            seq_entry.set_text("default")

        # Tool use
        self._tool_toggle.set_active(self._options.tool_use_enabled)
        parser = self._options.tool_call_parser
        if parser and parser in _PARSERS:
            self._parser_combo.set_active(_PARSERS.index(parser))
        else:
            self._parser_combo.set_active(0)
        self._auto_tool_check.set_active(self._options.enable_auto_tool_choice)
        tool_detail_visible = self._options.tool_use_enabled
        self._parser_combo.set_visible(tool_detail_visible)
        self._auto_tool_check.set_visible(tool_detail_visible)

        # General
        self._dev_mode_check.set_active(self._options.dev_mode)
        self._no_timeout_check.set_active(self._options.disable_metal_timeout)
        self._workflow_entry.set_text(self._options.workflow_args)

        # Advanced
        self._vllm_extra_entry.set_text(self._options.extra_vllm_args)
        self._tt_config_entry.set_text(self._options.override_tt_config)
        self._hf_cache_entry.set_text(self._options.host_hf_cache)
        self._volume_entry.set_text(self._options.host_volume)
        self._weights_entry.set_text(self._options.host_weights_dir)
        self._bind_entry.set_text(self._options.bind_host)
        self._device_id_entry.set_text(self._options.device_id)
        self._image_user_entry.set_text(self._options.image_user)
        self._skip_sw_check.set_active(self._options.skip_system_sw_validation)
        self._no_trace_check.set_active(self._options.disable_trace_capture)

    # ----------------------------------------------------------- signal handlers

    def _on_uc_toggled(self, btn: Gtk.ToggleButton, use_case: str) -> None:
        if self._inhibit_signals or not btn.get_active():
            return
        self._apply_use_case(use_case)
        self._schedule_preview_update()
        self._on_options_changed(self._options)

    def _on_ctx_changed(self, entry: Gtk.Entry) -> None:
        if self._inhibit_signals:
            return
        text = entry.get_text().strip()
        if text == "default":
            self._options.max_model_len = None
        else:
            try:
                self._options.max_model_len = int(text)
            except ValueError:
                pass
        self._deselect_use_case_chip()
        self._schedule_preview_update()

    def _on_seq_changed(self, entry: Gtk.Entry) -> None:
        if self._inhibit_signals:
            return
        text = entry.get_text().strip()
        if text == "default":
            self._options.max_num_seqs = None
        else:
            try:
                self._options.max_num_seqs = int(text)
            except ValueError:
                pass
        self._deselect_use_case_chip()
        self._schedule_preview_update()

    def _on_tool_toggled(self, btn: Gtk.ToggleButton) -> None:
        if self._inhibit_signals:
            return
        enabled = btn.get_active()
        self._options.tool_use_enabled = enabled
        self._parser_combo.set_visible(enabled)
        self._auto_tool_check.set_visible(enabled)
        if enabled and self._entry and not self._options.tool_call_parser:
            self._options.tool_call_parser = detect_tool_parser(self._entry)
            idx = _PARSERS.index(self._options.tool_call_parser) if self._options.tool_call_parser in _PARSERS else 0
            self._parser_combo.set_active(idx)
        self._deselect_use_case_chip()
        self._schedule_preview_update()

    def _on_parser_changed(self, combo: Gtk.ComboBoxText) -> None:
        if self._inhibit_signals:
            return
        self._options.tool_call_parser = combo.get_active_text() or ""
        self._schedule_preview_update()

    def _on_any_change(self, widget) -> None:
        if self._inhibit_signals:
            return
        self._read_widgets_to_options()
        self._deselect_use_case_chip()
        self._schedule_preview_update()
        self._on_options_changed(self._options)

    def _on_vllm_extra_changed(self, entry: Gtk.Entry) -> None:
        if self._inhibit_signals:
            return
        text = entry.get_text().strip()
        self._options.extra_vllm_args = text
        # Validate JSON — show red border when invalid
        if text:
            try:
                json.loads(text)
                entry.remove_css_class("error")
            except json.JSONDecodeError:
                entry.add_css_class("error")
        else:
            entry.remove_css_class("error")
        self._schedule_preview_update()

    def _on_docker_changed(self, combo: Gtk.ComboBoxText) -> None:
        if self._inhibit_signals:
            return
        active_id = combo.get_active_id()
        if active_id == "__spec__":
            self._options.docker_image_override = ""
        else:
            # active_id is the repo_tag for non-spec entries
            self._options.docker_image_override = active_id or ""
        self._schedule_preview_update()

    def _on_docker_refresh(self, _btn) -> None:
        if self._entry:
            self._refresh_docker_images(self._entry.docker_image)

    # --------------------------------------------------------- docker images

    def _refresh_docker_images(self, spec_default: str = "") -> None:
        self._docker_status.set_text("scanning…")
        def _scan():
            images = scan_local_images(spec_default)
            idle_add_once(self._populate_docker_combo, images, spec_default)
        threading.Thread(target=_scan, daemon=True).start()

    def _populate_docker_combo(
        self, images: List[DockerImage], spec_default: str
    ) -> None:
        self._docker_images = images
        self._inhibit_signals = True
        # Remove all but the placeholder
        while self._docker_combo.get_model().iter_n_children(None) > 1:
            self._docker_combo.remove(1)
        for img in images:
            if img.repo_tag == spec_default:
                label = f"{img.short_tag}  ·  {img.size_str}  ·  {img.created_str}  (spec default)"
            else:
                label = f"{img.short_tag}  ·  {img.size_str}  ·  {img.created_str}"
            self._docker_combo.append(img.repo_tag, label)
        # Select spec-default entry if present
        if images and images[0].repo_tag == spec_default:
            self._docker_combo.set_active(1)
            pulled = images[0].created_str != "not pulled"
            self._docker_status.set_text("✓ pulled" if pulled else "✗ not pulled")
        else:
            self._docker_combo.set_active(0)
            self._docker_status.set_text(f"{len(images)} local images")
        self._inhibit_signals = False

    # ------------------------------------------------------------ profiles

    def _refresh_profiles(self, model_type: str) -> None:
        self._inhibit_signals = True
        while self._profile_combo.get_model().iter_n_children(None) > 1:
            self._profile_combo.remove(1)
        for p in list_profiles(model_type):
            name = p.get("name", "")
            desc = p.get("description", "")
            label = f"{name}  —  {desc}" if desc else name
            self._profile_combo.append(name, label)
        self._profile_combo.set_active(0)
        self._del_profile_btn.set_sensitive(False)
        self._inhibit_signals = False

    def _on_profile_selected(self, combo: Gtk.ComboBoxText) -> None:
        if self._inhibit_signals:
            return
        active_id = combo.get_active_id()
        if active_id == "__none__" or not active_id:
            self._del_profile_btn.set_sensitive(False)
            return
        self._del_profile_btn.set_sensitive(True)
        from profiles import load_profile
        p = load_profile(active_id)
        if p and self._entry:
            self._inhibit_signals = True
            self._options = profile_to_options(p)
            self._options.use_case = p.get("options", {}).get("use_case", "chat")
            self._sync_widgets_to_options()
            # Highlight matching chip if any
            for uc, btn in self._use_case_btns.items():
                btn.handler_block_by_func(self._on_uc_toggled)
                btn.set_active(uc == self._options.use_case)
                btn.handler_unblock_by_func(self._on_uc_toggled)
            self._inhibit_signals = False
            self._update_preview()
            self._on_options_changed(self._options)

    def _on_save_profile(self, _btn) -> None:
        if self._entry is None:
            return
        dialog = Gtk.Dialog(title="Save Profile", transient_for=self.get_root(), modal=True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Save", Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(6)
        box.set_margin_start(12); box.set_margin_end(12)
        box.set_margin_top(8);   box.set_margin_bottom(8)
        name_entry = Gtk.Entry()
        name_entry.set_placeholder_text("Profile name (no spaces)")
        box.append(Gtk.Label(label="Name:"))
        box.append(name_entry)
        desc_entry = Gtk.Entry()
        desc_entry.set_placeholder_text("Optional description")
        box.append(Gtk.Label(label="Description:"))
        box.append(desc_entry)
        dialog.present()

        def _on_response(dlg, resp):
            if resp == Gtk.ResponseType.OK:
                name = name_entry.get_text().strip().replace(" ", "-")
                desc = desc_entry.get_text().strip()
                if name and self._entry:
                    save_profile(name, desc, self._entry.model_type, self._options)
                    self._refresh_profiles(self._entry.model_type)
            dlg.destroy()

        dialog.connect("response", _on_response)

    def _on_delete_profile(self, _btn) -> None:
        active_id = self._profile_combo.get_active_id()
        if not active_id or active_id == "__none__":
            return
        delete_profile(active_id)
        if self._entry:
            self._refresh_profiles(self._entry.model_type)

    # ---------------------------------------------------------- options sync

    def _read_widgets_to_options(self) -> None:
        """Sync all editable widget values into self._options."""
        opts = self._options
        opts.dev_mode = self._dev_mode_check.get_active()
        opts.disable_metal_timeout = self._no_timeout_check.get_active()
        opts.workflow_args = self._workflow_entry.get_text().strip()
        opts.extra_vllm_args = self._vllm_extra_entry.get_text().strip()
        opts.override_tt_config = self._tt_config_entry.get_text().strip()
        opts.host_hf_cache = self._hf_cache_entry.get_text().strip()
        opts.host_volume = self._volume_entry.get_text().strip()
        opts.host_weights_dir = self._weights_entry.get_text().strip()
        opts.bind_host = self._bind_entry.get_text().strip()
        opts.device_id = self._device_id_entry.get_text().strip()
        opts.image_user = self._image_user_entry.get_text().strip()
        opts.skip_system_sw_validation = self._skip_sw_check.get_active()
        opts.disable_trace_capture = self._no_trace_check.get_active()
        opts.tool_use_enabled = self._tool_toggle.get_active()
        opts.enable_auto_tool_choice = self._auto_tool_check.get_active()
        opts.tool_call_parser = self._parser_combo.get_active_text() or ""

    def _deselect_use_case_chip(self) -> None:
        """Deselect all use-case chips (user edited a field that contradicts the preset)."""
        for btn in self._use_case_btns.values():
            if btn.get_active():
                btn.handler_block_by_func(self._on_uc_toggled)
                btn.set_active(False)
                btn.handler_unblock_by_func(self._on_uc_toggled)

    def get_options(self) -> LaunchOptions:
        return self._options

    # -------------------------------------------------------- command preview

    def _schedule_preview_update(self) -> None:
        if self._preview_source:
            GLib.source_remove(self._preview_source)
        self._preview_source = GLib.timeout_add(150, self._update_preview_cb)

    def _update_preview_cb(self) -> bool:
        self._preview_source = None
        self._update_preview()
        return False

    def _update_preview(self) -> None:
        if self._entry is None:
            self._preview_buf.set_text("")
            return
        e = self._entry
        base = [
            "python3 run.py",
            f"--model {e.display_name}",
            "--workflow server --docker-server",
            f"--service-port 8000",
            f"--tt-device {e.device_type.lower()}",
            "--no-auth",
        ]
        if self._options.docker_image_override:
            base.append(f"--override-docker-image {self._options.docker_image_override}")

        class _E:
            inference_engine = e.inference_engine
            family = e.family
        extra = build_extra_args(self._options, _E())
        all_parts = base + extra
        self._preview_buf.set_text(" \\\n  ".join(all_parts))
```

- [ ] **Step 2: Verify the file parses cleanly**

```bash
cd /home/ttuser/code/tt-model-runner-gui
python3 -c "import sys; sys.path.insert(0,'app'); import config_panel; print('OK')"
```

Expected: `OK` (no import errors).

- [ ] **Step 3: Commit**

```bash
git add app/config_panel.py
git commit -m "feat: add ConfigPanel GTK widget — use-case chips, quick settings, docker picker, command preview"
```

---

## Task 6: `app/main_window.py` — integrate Gtk.Stack + ConfigPanel

**Files:**
- Modify: `app/main_window.py` (multiple sections)

- [ ] **Step 1: Add Gtk.Stack to `MainPanel._build()`**

In `MainPanel._build()` (around line 424), currently the method appends:
1. Banner box
2. Separator
3. Stepper revealer
4. Progress revealer
5. Tour revealer
6. Separator
7. Filter bar
8. Separator
9. Log scroll

Refactor so steps 7-9 move inside a Gtk.Stack "logs" page. The code currently ends at line ~549. Make the following change — replace everything from `# Log filter toolbar` to the end of `_build()`:

```python
        # Stack — holds welcome / config / logs pages
        self._stack = Gtk.Stack()
        self._stack.set_vexpand(True)
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(150)

        # Welcome page
        welcome_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        welcome_box.set_valign(Gtk.Align.CENTER)
        welcome_box.set_halign(Gtk.Align.CENTER)
        welcome_lbl = Gtk.Label(label="Select a model to configure and launch")
        welcome_lbl.add_css_class("muted")
        welcome_box.append(welcome_lbl)
        self._stack.add_named(welcome_box, "welcome")

        # Config page — lazy, created on first set_model() call
        self._config_panel: Optional["ConfigPanel"] = None

        # Logs page — filter bar + log view
        logs_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        filter_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        filter_bar.set_margin_start(8); filter_bar.set_margin_end(8)
        filter_bar.set_margin_top(3);   filter_bar.set_margin_bottom(3)
        filter_lbl = Gtk.Label(label="Filter:")
        filter_lbl.add_css_class("muted")
        filter_bar.append(filter_lbl)
        self._filter_btns: dict = {}
        for lvl in _LOG_LEVELS_ORDERED:
            btn = Gtk.ToggleButton(label=lvl)
            btn.set_active(True)
            btn.add_css_class("log-filter-btn")
            btn.connect("toggled", self._on_filter_toggled, lvl)
            filter_bar.append(btn)
            self._filter_btns[lvl] = btn
        self._log_count_lbl = Gtk.Label(label="")
        self._log_count_lbl.add_css_class("muted")
        self._log_count_lbl.set_hexpand(True)
        self._log_count_lbl.set_halign(Gtk.Align.END)
        filter_bar.append(self._log_count_lbl)
        logs_box.append(filter_bar)
        logs_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        log_scroll = Gtk.ScrolledWindow()
        log_scroll.set_vexpand(True)
        log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        self._log_buf = Gtk.TextBuffer()
        for level, color in _LOG_COLORS.items():
            self._log_buf.create_tag(f"lvl_{level}", foreground=color)
        self._log_buf.create_tag("ts", foreground="#4FD1C5")

        self._log_view = Gtk.TextView(buffer=self._log_buf)
        self._log_view.set_editable(False)
        self._log_view.set_cursor_visible(False)
        self._log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._log_view.add_css_class("log-view")
        self._log_view.set_monospace(True)

        self._vadj = log_scroll.get_vadjustment()
        self._vadj.connect("value-changed", self._on_scroll)
        self._vadj.connect("changed",       self._on_adj_changed)

        log_scroll.set_child(self._log_view)
        logs_box.append(log_scroll)
        self._stack.add_named(logs_box, "logs")

        self.append(self._stack)
```

- [ ] **Step 2: Add `show_config`, `show_logs`, `show_welcome` methods to `MainPanel`**

Add these methods to `MainPanel` (after `_build`, before `_on_scroll`):

```python
    def show_welcome(self) -> None:
        self._stack.set_visible_child_name("welcome")

    def show_config(self, entry: "ModelEntry", on_options_changed: Callable) -> None:
        from config_panel import ConfigPanel
        if self._config_panel is None:
            self._config_panel = ConfigPanel(on_options_changed)
            self._stack.add_named(self._config_panel, "config")
        self._config_panel.set_model(entry)
        self._stack.set_visible_child_name("config")

    def show_logs(self) -> None:
        self._stack.set_visible_child_name("logs")

    def get_options(self) -> Optional["LaunchOptions"]:
        if self._config_panel is not None:
            return self._config_panel.get_options()
        return None
```

Add the import at the top of the `MainPanel` class (or at module level):
```python
from typing import Callable, List, Optional, TYPE_CHECKING
if TYPE_CHECKING:
    from config_panel import ConfigPanel
    from launch_options import LaunchOptions
```

- [ ] **Step 3: Update `MainWindow._on_model_select`**

Find `_on_model_select` (line ~756). After the existing code that sets `_cache_info` and `_banner_info`, add:

```python
    def _on_model_select(self, entry: ModelEntry):
        self._current_entry = entry
        self._cache_info = None
        self._panel._banner_info.set_text(
            f"{entry.display_name}  ·  {entry.device_type}  ·  {entry.inference_engine}"
        )
        # Show config panel for this model
        self._panel.show_config(entry, self._on_options_changed)

        repo = entry.hf_model_repo
        def _scan():
            info = scan_model_cache(repo)
            idle_add_once(self._on_cache_scanned, info)
        threading.Thread(target=_scan, daemon=True).start()
```

Add `_on_options_changed` callback to `MainWindow`:

```python
    def _on_options_changed(self, options) -> None:
        pass   # reserved for future use (e.g. validate JSON fields, enable/disable launch btn)
```

- [ ] **Step 4: Update `MainWindow._on_launch` to read options from panel**

Find `_on_launch` (line ~788). Replace the `config = LaunchConfig(...)` block:

```python
        options = self._panel.get_options()
        config = LaunchConfig(
            repo_path=repo_path,
            model_name=entry.display_name,
            device=entry.device_type,
            port=port,
            hf_token=hf_token,
            no_auth=True,
            options=options,
            inference_engine=entry.inference_engine,
        )
```

After `self._transition(ServerState.LAUNCHING)`, switch to logs:

```python
        self._panel.show_logs()
        self._transition(ServerState.LAUNCHING)
```

- [ ] **Step 5: Update `MainWindow._transition` to switch back to config on IDLE/ERROR**

In `_transition` (line ~869), add stack switching:

```python
    def _transition(self, state: ServerState):
        if state == self._state:
            return
        prev = self._state
        self._state = state

        # ...existing code...

        # Switch main panel page
        if state in (ServerState.IDLE, ServerState.ERROR):
            if self._current_entry:
                self._panel.show_config(self._current_entry, self._on_options_changed)
            else:
                self._panel.show_welcome()
        elif state == ServerState.LAUNCHING:
            self._panel.show_logs()
```

The `show_logs()` call in `_on_launch` is kept; the `_transition` check is a safety net.

- [ ] **Step 6: Run the full test suite**

```bash
python -m pytest tests/ -v 2>&1 | tail -20
```

Expected: all tests PASS.

- [ ] **Step 7: Start the app and manually verify the config panel appears**

```bash
cd /home/ttuser/code/tt-model-runner-gui
python3 app/main.py &
```

1. Select a model from the sidebar — the right panel should switch to the ConfigPanel
2. Click a use-case chip — quick settings should fill in
3. Toggle "Tool use" on — parser dropdown should appear
4. Check "Docker image" row — should show "scanning…" then populate
5. Click "▶ Launch Server" — panel should switch to log view
6. Stop server — panel should switch back to ConfigPanel

- [ ] **Step 8: Commit**

```bash
git add app/main_window.py
git commit -m "feat: integrate ConfigPanel into MainWindow via Gtk.Stack — config page on model-select, logs on launch"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task |
|-----------------|------|
| Use-case presets per model type | Task 1 (PRESETS, MODEL_TYPE_USE_CASES) |
| LaunchOptions dataclass (all fields) | Task 1 |
| Tool-parser auto-detection | Task 1 (detect_tool_parser) |
| CLI arg builder, vLLM JSON merge priority | Task 1 (build_extra_args) |
| `scan_local_images()` | Already done + tested in Task 3 |
| Spec-default image placement | Task 3 (test verifies it) |
| ConfigPanel layout (model strip, profile bar, chips, quick settings, docker, advanced, preview) | Task 5 |
| vLLM row hidden for non-vLLM engines | Task 5 (_vllm_box.set_visible) |
| Context/seqs dropdowns with "default" option | Task 5 |
| Tool use toggle + parser dropdown (appears on ON) | Task 5 |
| Command preview with 150ms debounce | Task 5 (_schedule_preview_update) |
| Profiles save/load/delete | Task 2 + Task 5 (profile bar wired to profiles.py) |
| Gtk.Stack welcome/config/logs | Task 6 |
| model-select → config page | Task 6 (_on_model_select) |
| launch → logs page | Task 6 (_on_launch + _transition) |
| IDLE/ERROR → back to config | Task 6 (_transition) |
| LaunchOptions wired into LaunchConfig → server_manager.launch() | Task 4 |
| docker_image_override in options propagated to --override-docker-image | Task 4 |
| GHCR auto-resolution (already implemented) | Already done |
| Missing-dep auto-install (already implemented) | Already done |

**No placeholders found.**

**Type consistency:** `LaunchOptions` defined in Task 1, used in Task 2 (`profiles.py`), Task 4 (`server_manager`), Task 5 (`config_panel`), Task 6 (`main_window`). All usages reference `LaunchOptions` from `launch_options` module. `build_extra_args` signature consistent across Task 1 definition and Task 4 + Task 5 calls. `_EntryProxy` in Task 4 duck-types correctly for `inference_engine` and `family` attributes.
