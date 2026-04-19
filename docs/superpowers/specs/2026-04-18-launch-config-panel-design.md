# Launch Configuration Panel Design

## Goal

After selecting a model, present a full-screen configuration panel (replacing the log view when idle) that lets users pick a semantic use-case preset, tune any launch-time setting, select a local Docker image, and optionally save the configuration as a named profile for reuse.

## Architecture

### New files

| File | Responsibility |
|------|---------------|
| `app/launch_options.py` | `UseCase` definitions per model type, `LaunchOptions` dataclass, preset → options mapping, tool-parser auto-detection, CLI arg builder |
| `app/docker_images.py` | `scan_local_images()` — runs `docker images`, returns TT-relevant images with size/age metadata |
| `app/config_panel.py` | `ConfigPanel(Gtk.Box)` widget — use-case chips, quick settings, advanced expander, command preview, profile save/load |

### Modified files

| File | Change |
|------|--------|
| `app/server_manager.py` | `LaunchConfig` gains `options: LaunchOptions`; `launch()` merges options into the command |
| `app/main_window.py` | `MainPanel` gains a `Gtk.Stack` with a `"config"` page and a `"logs"` page; model-select switches to config, launch switches to logs |
| `app/app_settings.py` | No schema change — profiles stored separately under `~/.config/tt-runner-gui/profiles/` |

---

## `launch_options.py`

### Use-case presets (per `ModelType`)

| Model type | Presets |
|-----------|---------|
| LLM, VLM | Chat · Code completion · Agent frameworks · Deep research · Creative writing · Dev |
| AUDIO | Sound analysis · Dev |
| TEXT_TO_SPEECH | MIDI generation · Music generation · Dev |
| IMAGE | Creative · Dev |
| VIDEO | Creative · Dev |
| EMBEDDING | Semantic search · RAG pipeline · Dev |
| CNN | Standard · Dev |

### LLM preset → field mapping

| Preset | `max_model_len` | `max_num_seqs` | Tool use | Other |
|--------|----------------|---------------|----------|-------|
| Chat | spec default | spec default | OFF | — |
| Code completion | 32 768 | spec × 2 | OFF | — |
| Agent frameworks | spec max | spec default | ON, auto | `enable_auto_tool_choice` |
| Deep research | spec max | 4 | OFF | — |
| Creative writing | 65 536 | 4 | OFF | — |
| Dev | spec default | spec default | OFF | `dev_mode`, `disable_metal_timeout`, `disable_trace_capture` |

All other model types have only straightforward presets (no vLLM-specific fields): Dev enables `dev_mode` + `disable_metal_timeout`.

### `LaunchOptions` dataclass

```python
@dataclass
class LaunchOptions:
    use_case: str = "chat"

    # vLLM quick settings (None = use spec default)
    max_model_len: Optional[int] = None
    max_num_seqs: Optional[int] = None

    # Tool use (vLLM only)
    tool_use_enabled: bool = False
    tool_call_parser: str = ""         # empty = auto-detect from model family
    enable_auto_tool_choice: bool = False
    extra_vllm_args: str = ""          # freeform JSON merged last

    # General
    dev_mode: bool = False
    disable_metal_timeout: bool = False
    disable_trace_capture: bool = False

    # Docker
    docker_image_override: str = ""    # empty = spec default

    # Advanced (all map directly to run.py flags)
    workflow_args: str = ""
    override_tt_config: str = ""       # JSON string
    host_hf_cache: str = ""
    host_volume: str = ""
    host_weights_dir: str = ""
    bind_host: str = ""
    device_id: str = ""
    image_user: str = ""               # default "1000"
    skip_system_sw_validation: bool = False
```

### Tool-parser auto-detection

```python
_FAMILY_TO_PARSER = {
    "Llama":    "llama3_json",
    "Qwen":     "hermes",
    "Mistral":  "mistral",
    "DeepSeek": "hermes",
    "Gemma":    "hermes",
    "QwQ":      "hermes",
}
# Falls back to "hermes" for unknown families.
```

`detect_tool_parser(entry: ModelEntry) -> str` returns the parser name, and marks it `(auto-detected)` in the UI.

### CLI arg builder

`build_extra_args(options: LaunchOptions, entry: ModelEntry) -> List[str]`

Produces the incremental flags appended to the base command. The base command (model, workflow, device, port, no-auth) is built by `ServerManager`; this function adds everything else. Merging priority for vLLM JSON:

1. Spec defaults (from `device_model_spec.vllm_args`)
2. `max_model_len` / `max_num_seqs` from options (override spec)
3. Tool-use args (added when `tool_use_enabled`)
4. `extra_vllm_args` JSON (merged last, wins over everything)

Final merged dict is serialised as `--vllm-override-args '{"key": value, ...}'`.

---

## `docker_images.py`

```python
@dataclass
class DockerImage:
    repo_tag: str       # "ghcr.io/tenstorrent/.../vllm-tt-metal:v0.56.0"
    size_str: str       # "4.21 GB"
    created_str: str    # "3 days ago"
    is_tt: bool         # matches tenstorrent / tt-inference / tt-metal / tt-media
```

`scan_local_images() -> List[DockerImage]`  
Runs `docker images --format '{{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}'`, parses output, filters to `is_tt = True`, sorts by creation date descending. Returns `[]` (not an error) if docker is not installed.

The spec's default image is matched against the list by prefix; if found, it is placed first and labelled `(spec default · pulled)`. If not found, it is synthesised as a placeholder entry labelled `(spec default · not pulled)` and shown greyed-out.

---

## `config_panel.py` — `ConfigPanel` widget

### Layout (top → bottom inside the main-panel config page)

```
┌──────────────────────────────────────────────────────┐
│ Model strip: name · type · engine · device · status  │
├──────────────────────────────────────────────────────┤
│ Profile bar: [ saved profiles dropdown ] [Save…] [✕] │
├──────────────────────────────────────────────────────┤
│ USE CASE  [Chat] [Code] [Agent] [Research] [Dev] …   │
├──────────────────────────────────────────────────────┤
│ QUICK SETTINGS (vLLM)                                │
│  Context length ▾   Max concurrent ▾                 │
│  [🔧 Tool use ON/OFF]  Parser: llama3_json ▾  [auto] │
├──────────────────────────────────────────────────────┤
│ QUICK SETTINGS (media / all)                         │
│  [Dev mode]  [Disable TT timeout]  Workflow args…    │
├──────────────────────────────────────────────────────┤
│ Docker image  [ghcr.io/.../vllm:v0.56 ▾] [Refresh]  │
│  ✓ pulled · 4.2 GB · 3 days ago                      │
├──────────────────────────────────────────────────────┤
│ ▸ Advanced                                           │
│   vLLM JSON · TT config JSON · HF cache · Volume     │
│   Weights dir · Bind host · Device ID · Image UID    │
│   [skip SW validation] [disable trace capture]       │
├──────────────────────────────────────────────────────┤
│ COMMAND PREVIEW (always visible, monospace, wraps)   │
│  python3 run.py --model … --vllm-override-args '…'  │
└──────────────────────────────────────────────────────┘
```

### Behaviour details

**Use-case chips:** Clicking a chip calls `_apply_preset(use_case)` which fills all Quick Settings fields. Fields remain editable after the preset is applied — the chip just shortcut-fills them. The selected chip stays highlighted; it deselects if the user manually edits any field that would contradict it (no silent re-snap).

**Quick settings visibility:** The vLLM-specific row (context, concurrency, tool use) is only shown when `entry.inference_engine == "vllm"`. The media/general row (dev mode, timeout, workflow args) is shown for all engines. The Advanced expander is shown for all.

**Context length dropdown:** Offers `[8 192, 16 384, 32 768, 65 536, 131 072, "spec max (N)"]` where N comes from `entry`'s spec. User can also type a custom value directly.

**Max concurrent dropdown:** Offers `[1, 4, 8, 16, 32, 64, "spec default (N)"]`. Editable.

**Tool use:** Toggle button. When ON: parser dropdown appears (auto-detected, editable); `enable_auto_tool_choice` checkbox appears. Parser dropdown lists all known parsers + "(auto)" at top.

**Docker image picker:** `Gtk.ComboBox` populated from `scan_local_images()` (run in a background thread on panel show; a Refresh button re-runs it). Each row shows `repo:tag (size · age)`. If spec default is not found locally, it's shown as the first item greyed out. Selecting it leaves `docker_image_override` empty (spec default behaviour preserved).

**Command preview:** A `Gtk.TextView` (non-editable, monospace, word-wrap) that rebuilds from `build_extra_args()` on every field change. Updates are debounced 150 ms to avoid thrashing during typing.

**By-value control:** Every field (including those set by presets) is directly editable with no restrictions. There is no lock or disable after preset selection. This satisfies completist control.

---

## Profiles

### Storage

`~/.config/tt-runner-gui/profiles/<profile-name>.json` — one file per profile. Profiles are global (not per-model) but can be filtered by model type in the UI.

### Profile JSON format

```json
{
  "name": "agent-llama-70b",
  "description": "Llama 70B with tool use for AutoGen agents",
  "model_type": "LLM",
  "created": "2026-04-18T12:00:00",
  "options": { ...LaunchOptions fields... }
}
```

### Profile bar (below model strip)

- **Dropdown** populated from `~/.config/tt-runner-gui/profiles/` filtered to matching `model_type` plus all profiles. Shows `"No profile"` when nothing is loaded.
- **Save…** button: opens a small inline dialog (Gtk.Popover) asking for a name and optional description. Saves current options as a profile file. If a profile with that name exists, confirms overwrite.
- **Delete (✕)**: removes the currently loaded profile file after confirmation.
- Loading a profile fills all Quick Settings and Advanced fields with the saved values and sets the use-case chip to match.

---

## `MainPanel` stack change

`MainPanel` gains a `Gtk.Stack` at the centre (replacing the current log-only layout):

- `"welcome"` page: shown when no model is selected (current "Select a model" message)
- `"config"` page: `ConfigPanel` instance, created lazily on first model select
- `"logs"` page: existing log view + filter toolbar

`MainWindow._on_model_select()` → switch to `"config"`.  
`MainWindow._transition(LAUNCHING)` → switch to `"logs"`.  
`MainWindow._transition(IDLE or ERROR)` → switch back to `"config"` (if model still selected).

The status banner, stepper, progress bar, and tour panel remain above the stack (they attach to the log page naturally since they reveal/hide themselves).

Actually, stepper/progress/tour only make sense during loading, not during config. The stack will sit *below* those revealers; they already hide themselves during IDLE.

---

## `server_manager.py` changes

`LaunchConfig` gains:

```python
options: Optional[LaunchOptions] = None
```

`launch()` calls `build_extra_args(config.options, ...)` and appends the result to `cmd`. If `options` is `None`, uses defaults (current behaviour, no regression).

---

## Error handling

- `docker images` failure (docker not installed / permission denied): docker picker shows a single disabled entry `"docker not available"`. Log a warning, do not crash.
- Invalid JSON in vLLM override or TT config fields: show inline red border + tooltip `"Invalid JSON"` and disable the Launch button until fixed.
- Profile file read error: log and skip the corrupt profile, do not crash.

---

## Testing

| Test file | Coverage |
|-----------|----------|
| `tests/test_launch_options.py` | `build_extra_args` for each use case + edge cases; `detect_tool_parser`; JSON merge priority; `None` defaults passthrough |
| `tests/test_docker_images.py` | `scan_local_images` with mocked subprocess; TT-image filter; spec-default match logic |
| `tests/test_profiles.py` | save/load/delete round-trip; corrupt-file tolerance; model_type filter |

No GTK widget tests — the widget layer is thin wiring over the pure-logic modules above.
