# tt-model-runner-gui Design Spec
**Date:** 2026-04-18  
**Status:** Approved

---

## Overview

A GTK4 desktop application for managing the lifecycle of `tt-inference-server` model deployments. The user picks a model, picks their hardware, launches a Docker-backed inference server, and watches it come up — with live logs, intelligent progress bars, and health monitoring. All actual inference interaction is left to other tools (e.g. tt-local-generator).

**Out of scope:** prompt submission, completions API, benchmarks, evals, multi-host deployments.

---

## Approach

**Hybrid integration with tt-inference-server:**
- Parse `model_spec.json` directly (JSON only, no Python import) for the model catalog and model metadata
- Import `workflows/workflow_types.py` enums for `DeviceTypes` and `ModelType` (stable, low-churn)
- Launch `run.py` as a subprocess for the actual server start — all setup_host, validate_setup, secrets logic stays in the CLI
- Tail the log file `run.py` produces for status and progress
- Health-check independently via `GET localhost:{port}/v1/models`
- Stop the server via `docker stop <container_name>` independently

---

## File Structure

```
tt-model-runner-gui/
  app/
    main.py              # Gtk.Application entry point, CSS loading, repo path resolution
    main_window.py       # ApplicationWindow: sidebar + main panel layout, all widgets
    model_catalog.py     # Parses model_spec.json → ModelEntry dataclass tree
    device_detector.py   # Runs tt-smi -s, returns list of compatible DeviceTypes
    server_manager.py    # Launches run.py, finds+tails log file, stops container
    health_worker.py     # Background thread: polls /v1/models, emits state transitions
    timing_store.py      # Persistent load/download timing estimates with cross-model inference
    worker.py            # GLib.idle_add threading helpers (copied pattern from tt-local-generator)
    app_settings.py      # Persists last-used model/device/port to ~/.config/tt-runner-gui/settings.json
  run                    # Executable launcher (sets PYTHONPATH, launches app/main.py)
```

---

## CSS / Theming

Reuse the Tenstorrent dark palette verbatim from `tt-local-generator/app/main_window.py`:

```css
@define-color tt_bg_panel    #0A1F28;
@define-color tt_bg_darkest  #0F2A35;
@define-color tt_bg_dark     #1A3C47;
@define-color tt_border      #2D5566;
@define-color tt_accent      #4FD1C5;
@define-color tt_accent_light #81E6D9;
@define-color tt_text        #E8F0F2;
@define-color tt_text_muted  #607D8B;
@define-color tt_pink        #EC96B8;
@define-color tt_success     #27AE60;
@define-color tt_error       #FF6B6B;
```

State pill colors: IDLE=muted, LAUNCHING/PULLING/LOADING=accent (pulsing), READY=success, ERROR=error, STOPPING=pink.

---

## Window Layout

Two-panel `Gtk.Paned` (horizontal):

### Left Sidebar (~280px, fixed)

```
┌─────────────────────────────┐
│ [Server Repo path picker]   │  ← FileChooserButton, defaults to ~/code/tt-inference-server
├─────────────────────────────┤
│ MODEL                       │
│  ▾ LLM (42)                 │  ← Gtk.TreeView, 3 levels: Type → Family → Model
│    ▾ Llama                  │
│      ● Llama-3.3-70B-Inst.  │  ← selected row (bold, accent left-border)
│      ○ Llama-3.2-1B         │
│    ▸ Qwen                   │
│    ▸ Mistral                │
│  ▸ VLM (8)                  │
│  ▸ Image (6)                │
├─────────────────────────────┤
│ DEVICE                      │
│  [T3K] [N150] [P150] ...    │  ← toggle buttons, only compatible chips shown
├─────────────────────────────┤
│ PORT  [8000      ]          │  ← editable entry, default 8000
├─────────────────────────────┤
│ [▶ Launch Server         ]  │  ← prominent teal button; becomes [■ Stop] when running
├─────────────────────────────┤
│ HF_TOKEN: ✓ from env        │  ← status strip; red warning if missing
└─────────────────────────────┘
```

**Model tree behavior:**
- Device toggle selection filters the tree to show only models compatible with that device
- Selecting a model auto-selects the best compatible device if none chosen
- Tree nodes that have no compatible device given current hardware are grayed out
- Tree state (expanded groups) persists in `app_settings`

**Device detection:**
- `device_detector.py` calls `tt-smi -s` on startup (non-blocking background call)
- Only devices that appear in detection AND are valid for at least one model are shown
- If `tt-smi` unavailable: show all devices, add tooltip "tt-smi not found — showing all devices"

### Right Main Panel

```
┌──────────────────────────────────────────────────────┐
│  ● LOADING  │  localhost:8000  │  Llama-3.3-70B  T3K │  ← status banner
│  [████████████░░░░░░░░░░░]  ~4 min · based on Llama-3.1-70B on T3K (2 samples)  │
├──────────────────────────────────────────────────────┤
│  [Search logs...]  [▼ DEBUG ▼ INFO ▼ WARN ▼ ERROR]  [📋 Copy] [💾 Save]  │
├──────────────────────────────────────────────────────┤
│                                                      │
│  2026-04-18 12:01:03  INFO   Starting vLLM...        │
│  2026-04-18 12:01:05  INFO   Pulling docker image    │
│  2026-04-18 12:03:22  WARN   Warmup iteration 4/8    │
│  2026-04-18 12:05:11  INFO   ✅ Server ready         │
│                                                      │
└──────────────────────────────────────────────────────┘
```

- Status banner always visible; dims to muted when IDLE
- Progress bar: pulse when indeterminate (LAUNCHING/PULLING), time-based when estimate available, hidden when IDLE/READY/ERROR
- Estimate label updates live as elapsed time advances
- Log view: `Gtk.TextView` (read-only, monospace), color-coded by level using Pango tags
- Log level filter toggles hide/show lines without losing scroll position
- Auto-scroll to bottom unless user has scrolled up (same pattern as tt-local-generator)
- "Copy" copies visible (filtered) log to clipboard; "Save" prompts file save dialog

**Idle state main panel:** shows a "Select a model and click Launch" placeholder with the Tenstorrent wordmark.

---

## Server State Machine

```
IDLE → LAUNCHING → PULLING_IMAGE → LOADING → READY
                                      ↘ ERROR
       ↑                                     ↓
       └──────────── STOPPING ───────────────┘
```

| State | Progress Bar | Status Pill | Sidebar |
|---|---|---|---|
| IDLE | hidden | gray "IDLE" | fully interactive |
| LAUNCHING | pulse | teal "LAUNCHING" | model/device locked |
| PULLING_IMAGE | pulse | teal "PULLING IMAGE" | locked |
| LOADING | time-based (if estimate) or pulse | teal "LOADING" | locked |
| READY | full → fades out after 2s | green "READY" | locked (only Stop active) |
| ERROR | hidden | red "ERROR" | unlocked (re-launch allowed) |
| STOPPING | pulse | pink "STOPPING" | locked |

**Transitions triggered by:**
- IDLE → LAUNCHING: user clicks Launch
- LAUNCHING → PULLING_IMAGE: log line matches `docker pull`
- PULLING_IMAGE → LOADING: log line matches `Loading model weights` or `Starting vLLM`
- LOADING → READY: health worker gets HTTP 200 from `/v1/models`
- Any → ERROR: subprocess exits non-zero OR log line matches `ERROR` / `⛔` / `failed`
- READY/ERROR → STOPPING: user clicks Stop
- STOPPING → IDLE: `docker stop` completes OR process exits

---

## server_manager.py

```python
@dataclass
class LaunchConfig:
    repo_path: Path          # path to tt-inference-server
    model_name: str          # e.g. "meta-llama/Llama-3.3-70B-Instruct"
    device: str              # e.g. "T3K"
    port: str                # default "8000"
    hf_token: str | None     # from env; None → omit (model_spec may not need it)
    no_auth: bool = True     # always True (default flow)

class ServerManager:
    def launch(config: LaunchConfig, on_log_line: Callable, on_state: Callable)
    def stop()
    def get_container_name() -> str | None
```

**Launch sequence:**
1. Build `run.py` command: `python3 run.py --model X --workflow server --docker-server --no-auth --service-port P`
2. `Popen` with `stdout=DEVNULL, stderr=DEVNULL` (run.py writes its own log file)
3. Poll `workflow_logs/run_logs/` for a new `run_*.log` file (appears within ~2s of launch)
4. Background thread tails the log file line-by-line → `on_log_line(line)` → `GLib.idle_add`
5. Also emit structured state transitions based on recognized log patterns

**Stop sequence:**
1. `docker stop <container_name>` (non-blocking subprocess)
2. If container name unknown: `kill` the run.py subprocess
3. Join the tail thread

**Container name:** Detected via `docker ps --filter "ancestor={docker_image}" --format "{{.Names}}"` shortly after launch, or by parsing run.py's log line `"Docker run command:"` which includes the `--name` flag. Stored in `ServerManager` instance for use by Stop. If not found within 15s of launch, stop falls back to terminating the run.py subprocess.

---

## health_worker.py

Background thread (same pattern as `HealthWorker` in tt-local-generator):

```python
class HealthWorker(threading.Thread):
    def __init__(self, port, on_ready, on_lost, poll_interval=5.0): ...
    def stop(): ...
```

- Polls `GET http://localhost:{port}/v1/models` every 5s
- First successful response → `GLib.idle_add(on_ready, model_list)`
- Subsequent failure after READY → `GLib.idle_add(on_lost)`
- Starts polling as soon as LAUNCHING; first success drives LOADING→READY transition
- Displays loaded model names from `/v1/models` response in the status banner

---

## model_catalog.py

```python
@dataclass
class ModelEntry:
    model_id: str              # e.g. "id_llama3_..."
    model_name: str            # e.g. "Llama-3.3-70B-Instruct"
    hf_model_repo: str         # e.g. "meta-llama/Llama-3.3-70B-Instruct"
    model_type: str            # "LLM", "VLM", "IMAGE", etc.
    family: str                # extracted: "Llama", "Qwen", "Mistral", ...
    device_type: str           # "T3K", "N150", etc.
    inference_engine: str      # "vLLM", "media", "forge"
    docker_image: str
    status: str                # "EXPERIMENTAL", "FUNCTIONAL", "COMPLETE", "TOP_PERF"
    param_count: float | None  # billions, from model_spec
    min_disk_gb: float | None
    min_ram_gb: float | None

class ModelCatalog:
    def load(spec_path: Path) -> ModelCatalog
    def get_tree() -> dict[str, dict[str, list[ModelEntry]]]  # type→family→entries
    def get_compatible(device_types: list[str]) -> ModelCatalog  # filtered view
    def get_entry(model_name, device) -> ModelEntry | None
```

**Family extraction:** strip version suffixes and size tokens from the model name.  
`"Llama-3.3-70B-Instruct"` → `"Llama"`, `"Qwen3-8B"` → `"Qwen"`, `"DeepSeek-R1-Distill-Llama-70B"` → `"DeepSeek"`.

---

## device_detector.py

```python
def detect_devices() -> list[str]:
    """Run tt-smi -s, parse JSON, return list of DeviceTypes name strings present."""

def get_compatible_devices(catalog: ModelCatalog, detected: list[str]) -> list[str]:
    """Intersect detected chips with devices that appear in the catalog."""
```

`tt-smi -s` returns JSON with a `device_info` array. Each entry has `board_type` which maps to a DeviceType. Falls back gracefully: if tt-smi not found or fails, returns empty list (UI shows all catalog devices with tooltip warning).

---

## timing_store.py

**Persistence:** `~/.config/tt-runner-gui/timing.json`

**Schema:**
```json
{
  "schema_version": 1,
  "download_speed_mbps": [12.3, 11.8, 13.1, 14.0, 12.9],
  "load_samples": {
    "meta-llama/Llama-3.3-70B-Instruct_T3K_cold": [245, 238, 241],
    "meta-llama/Llama-3.3-70B-Instruct_T3K_warm": [118, 115]
  },
  "device_load_rate": {
    "T3K":  {"seconds_per_gb": 3.2, "sample_count": 6},
    "N150": {"seconds_per_gb": 11.5, "sample_count": 2}
  },
  "family_load_rate": {
    "Llama_T3K":  {"seconds_per_gb": 3.1, "sample_count": 4},
    "Qwen_N150":  {"seconds_per_gb": 10.8, "sample_count": 2}
  }
}
```

**Key definitions:**
- `load_samples` key: `{hf_model_repo}_{device}_{cold|warm}` — `cold` = first load of freshly downloaded weights, `warm` = subsequent loads (weights on disk)
- Max 10 samples per key (FIFO eviction)
- Download speed: max 5 samples, rolling

**Estimation cascade** (`estimate_load(hf_repo, device, cold, size_gb, family) → EstimateResult`):

1. **Exact match** — own key has ≥1 sample → trimmed mean of own samples
2. **Family + device** — `{family}_{device}` rate × `size_gb` → scale by size ratio relative to family's average size
3. **Device baseline** — `device_load_rate[device].seconds_per_gb × size_gb`
4. **Cross-device fallback** — scale from another device using a known tier ratio (T3K≈3×N150 throughput)
5. **None** — return `EstimateResult(seconds=None, confidence="none", source="no data")`

```python
@dataclass
class EstimateResult:
    seconds: float | None
    confidence: Literal["none", "low", "medium", "high"]
    source: str   # human label: "based on Llama-3.1-70B on T3K (2 samples)"

class TimingStore:
    def record_download(hf_repo: str, size_gb: float, duration_s: float)
    def record_load(hf_repo: str, device: str, duration_s: float, cold: bool)
    def estimate_download(size_gb: float) -> EstimateResult
    def estimate_load(hf_repo: str, device: str, cold: bool, size_gb: float, family: str) -> EstimateResult
```

**Progress bar rendering with estimate:**
- `elapsed / estimated_seconds`, capped at 0.95 until state transitions to READY
- If elapsed exceeds estimate: bar stays at 0.95, label shows "~overrun by Xs"
- Confidence drives label phrasing:
  - `none` → pulse bar, no label
  - `low` → "~X min (rough estimate)"
  - `medium` → "~X min · based on {source}"
  - `high` → "~X min ± Ys · {source} ({n} samples)"

---

## app_settings.py

Persists to `~/.config/tt-runner-gui/settings.json`:

```json
{
  "server_repo_path": "/home/user/code/tt-inference-server",
  "last_model": "meta-llama/Llama-3.3-70B-Instruct",
  "last_device": "T3K",
  "last_port": "8000",
  "tree_expanded_types": ["LLM"],
  "log_level_filters": ["DEBUG", "INFO", "WARN", "ERROR"],
  "window_width": 1200,
  "window_height": 800,
  "sidebar_width": 280
}
```

Loaded at startup, saved on change (model selection, device selection, port edit, window resize).

---

## First-Run Path Discovery

On first launch, if no `settings.json` exists yet, the app searches for a `tt-inference-server` repo in this order:
1. `~/code/tt-inference-server`
2. `~/tt-inference-server`
3. Any directory containing `run.py` + `model_spec.json` under `~/code/`

If found, that path is saved to settings and shown in the repo path picker. If not found, the sidebar shows a "Select server repo" prompt with a file chooser button and the model tree is empty until a valid path is chosen.

A valid repo path requires: `run.py`, `model_spec.json`, and `workflows/` all present.

---

## HF_TOKEN / Secrets Handling

1. At startup, check `os.environ.get("HF_TOKEN")`
2. If present: sidebar shows `HF_TOKEN: ✓ from env` in green
3. If absent: check `{server_repo_path}/.env` for `HF_TOKEN=...`
4. If still absent: sidebar shows `⚠ HF_TOKEN not set` in red, Launch button disabled, tooltip: "Set HF_TOKEN in your environment or create a .env file in the server repo"
5. `JWT_SECRET`: always pass `--no-auth` to `run.py` — no JWT handling in the GUI

---

## Log Parsing (for state transitions and progress)

Log format: `2026-04-18 12:01:03,123 - filename.py:42 - INFO: message`

Key patterns (regex):

| Pattern | Trigger |
|---|---|
| `docker pull` | → PULLING_IMAGE |
| `Loading model weights` / `Starting vLLM` | → LOADING, start load timer |
| `Warmup iteration (\d+)/(\d+)` | progress hint (supplementary to time estimate) |
| `Application startup complete` / `Server ready` / `✅` | supplementary READY signal |
| `ERROR` / `⛔` / `failed` / `exit code` | → ERROR |
| `Docker logs are streamed to: (.+)` | capture docker log path for secondary tailing |
| HuggingFace progress: `Downloading.*?(\d+\.\d+)%` | download progress (supplementary) |

Health worker drives the authoritative LOADING→READY transition; log patterns drive LAUNCHING→PULLING→LOADING.

---

## Error Handling

- **run.py not found / server repo path invalid**: show error dialog, prompt to re-select repo path
- **Docker not available**: detect via `docker info` at launch time; show informative error before attempting
- **tt-smi not available**: soft failure — show all devices with tooltip warning
- **Log file not found after 10s**: show warning in log view "Log file not found — subprocess may have failed"; check run.py exit code
- **Container stop timeout (30s)**: force kill, log warning
- **Health check never succeeds after log says ready**: stay in LOADING, add "(health check pending)" to status
- **model_spec.json parse error**: show error dialog with path, disable model tree

---

## Cold vs Warm Load Detection

A load is classified as **cold** if:
- The model's HuggingFace weights directory does not yet exist under the HF cache, OR
- No tensor cache directory exists under `{host_volume}/tt_metal_cache/` for this model+device

Otherwise it is **warm**. The app checks this before launch and passes the flag to `timing_store.record_load()`. Cold loads are typically 5–8× slower than warm on the same device (no tensor cache to reuse).

For estimation purposes: if we have warm samples but not cold, estimate cold = warm × 5 (conservative). If we have cold but not warm, estimate warm = cold × 0.2.

---

## Loading Sub-stages (Per Engine Type)

The LOADING state is broken into named sub-stages, each surfaced in the UI as a stepper bar above the progress bar. Each sub-stage has its own duration tracked in `timing_store`.

### vLLM models (LLM, VLM)

| Stage key | Log trigger | Label shown |
|---|---|---|
| `engine_init` | `Automatically detected platform tt` | Initializing vLLM engine |
| `device_setup` | `multidevice with N devices.*created` | Setting up TT device mesh |
| `loading_weights` | `Loading checkpoint shards` | Loading model weights |
| `kv_cache` | `Allocating kv caches` | Profiling & allocating KV cache |
| `api_startup` | `Starting vLLM API server` | Starting HTTP server |
| `trace_capture` | `Capturing traces:.*input_seq_len=(\d+)` | Capturing traces (deterministic progress: 10 known context lengths) |
| `ready` | health check 200 | Ready |

Trace capture is the best deterministic progress source for vLLM: the log emits a line per context length (`128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32640, 65408` = 10 steps). Progress = `completed_lengths / total_lengths`.

### Media server models (WAN 2.2, SkyReels, FLUX, Mochi)

| Stage key | Log trigger | Label shown |
|---|---|---|
| `device_init` | `Creating new Video service` | Starting media server |
| `mesh_created` | `Created mesh device with N devices` | Device mesh ready (NxN) |
| `loading_weights` | `Device.*: Loading model` | Loading checkpoint shards |
| `cache_loading` | `loading cache at.*/(transformer\|text_encoder\|vae)` | Loading TT tensor cache: {component} |
| `model_loaded` | `Model loaded successfully` | Model on device |
| `warmup` | `Wan22 inference\|run\] executed.*inference` | Running warmup inference {n}/{total} |
| `ready` | `All devices are warmed up and ready` + `/tt-liveness` 200 | Ready |

WAN 2.2 warmup shows `N%|... {n}/{total}` in log — extract for deterministic progress within the warmup sub-stage.

### Stepper UI

```
✓ Device init  ──  ✓ Mesh (2×2)  ──  ✓ Weights  ──  ✓ TT Cache  ──  ● Warmup 1/2  ──  ○ Ready
[████████████████████████░░░░░░░░░░░░░░░░]  ~57s remaining · based on 8 samples
```

Completed stages show a checkmark in `tt_success` color. Active stage pulses in `tt_accent`. Pending stages are muted. The progress bar below is scoped to the **current sub-stage** duration, not total — so it fills and empties as each stage completes rather than being one long crawl across all stages.

---

## Loading Tour + Contextual Education

A fixed-height panel (layout option C) sits between the progress strip and the log view during loading. It cycles through contextual content related to what's happening **right now** in the loading sequence. Content is organized into "cards" that auto-advance every 15 seconds or when the sub-stage changes.

### Card types

**1. Model file tree** — scan the HF cache directory for this model, render a tree with file sizes:
```
📁 Wan-AI/Wan2.2-T2V-A14B-Diffusers/  (37.4 GB)
  ├── transformer/         26.1 GB   40 × safetensor shards
  ├── text_encoder/         9.3 GB   CLIP-ViT-H
  ├── vae/                  0.5 GB   3D causal VAE
  ├── tokenizer/            0.1 MB   CLIPTokenizer, vocab 49408
  └── config.json                    model architecture
```
Parse `config.json` for architecture facts: num_layers, hidden_size, num_heads, context_length.

**2. Matrix math — contextual** — the centerpiece education feature. Each sub-stage gets its own matrix explanation tied to what is **actually happening** with the **real dimensions from this model**:

| Sub-stage | What to explain | Matrix visualization |
|---|---|---|
| `loading_weights` | "Right now: reading attention weight matrix Q_proj [5120 × 5120] from disk into DRAM" | Animated matrix fill, dims labeled |
| `device_setup` | "Distributing 4 copies of the weight tensors across 4 Blackhole dies via ethernet fabric" | 2×2 grid of chips, tensor split arrows |
| `kv_cache` | "Allocating KV cache: for 32k context, K and V each need [32768 × 128] × 80 layers = 53GB. Available: 2105 blocks × 64 tokens" | Block allocation diagram |
| `trace_capture` | "JIT compiling: for seq_len=1024, attention scores = Q [1024×128] × K^T [128×1024] → [1024×1024]. Softmax → × V [1024×128]" | QK^T animated, dims live |
| `warmup` (WAN 2.2) | "Denoising step: spatial attention on [32768 × 64] latent patch tokens across T=21 frames, H=60, W=104" | Spatial patch grid |
| `mesh_created` | "Tensor parallelism: Q_proj [5120×5120] split column-wise, each chip gets [5120×1280]" | Weight shard coloring |

Dimensions come from the model's parsed config, so they're always accurate for the specific model being loaded.

**3. "Did you know" architecture facts** — one-line facts about the model, auto-generated from config:
- "WAN 2.2 has 40 transformer layers, each processing 32,768 spatial tokens simultaneously"
- "The text encoder encodes your prompt into 512 token embeddings of dimension 5120"
- "At bfloat16, the transformer alone is 26GB — roughly 5,200 4K movie frames"

**4. TT Metal system context** — while Fabric/Metal is initializing:
- "TT Metal is opening 4 Blackhole P300 dies connected via PCIe and ethernet"
- "Pre-compiled firmware loaded from cache — skipping kernel compilation"

### Card rendering
**Layout choice: Option A** — stepper bar at top of main panel; below it a split tour panel (file tree left, education card right); log stream below that. The tour panel collapses to zero height when IDLE or READY so it doesn't clutter the running state.

Cards use a two-column layout inside the tour panel: left = visual (ASCII matrix, file tree, chip diagram), right = explanation text. Cards are `Gtk.Stack` with a slide transition. A dot-indicator (●●○○) shows position. User can click ← → to navigate manually or let it auto-advance.

---

## Bootstrap Timing from Existing Logs

On first run, if `timing.json` does not exist, the app scans known log directories for past successful server runs and pre-populates the timing store. This gives useful estimates immediately rather than starting cold.

Scan locations (in order): `~/.local/lib/tt-inference-server/workflow_logs/`, `~/share_with_ttclaw/tt-inference-server/workflow_logs/`, `~/tt-scratchpad/**/workflow_logs/`, and any configured server repo's `workflow_logs/`.

For each successful log (contains "Application startup complete"):
- Parse `model` and `device` from filename
- Find first timestamp matching "detected platform tt" or "Starting vLLM API server" → load_start
- Find timestamp of "Application startup complete" → ready_t
- Record duration in timing store with cold/warm classification inferred from duration (>60s = likely cold for 8B models)

**Pre-seeded baseline from this machine's logs (2026-04-18):**
```json
{
  "load_samples": {
    "Llama-3.1-8B-Instruct_p150_cold": [100, 100],
    "Llama-3.1-8B-Instruct_p150_warm": [12, 13, 20, 60],
    "Qwen3-8B_p150_cold": [100],
    "Wan2.2-T2V-A14B-Diffusers_p300x2_warm": [151, 177, 151, 137, 151, 145, 152],
    "Wan2.2-Animate-14B-Diffusers_p300x2_cold": [218]
  },
  "substage_samples": {
    "Wan2.2-T2V-A14B-Diffusers_p300x2_device_init": [13, 14, 15, 15, 13, 15, 15],
    "Wan2.2-T2V-A14B-Diffusers_p300x2_cache_loading": [16, 16, 6, 15, 14, 16],
    "Wan2.2-T2V-A14B-Diffusers_p300x2_warmup": [116, 138, 113, 112, 116, 111, 115]
  },
  "device_load_rate": {
    "p150": {"seconds_per_gb": 7.23, "sample_count": 7},
    "p300x2": {"seconds_per_gb": 5.5, "sample_count": 7}
  },
  "family_load_rate": {
    "Llama_p150": {"seconds_per_gb": 6.35, "sample_count": 6},
    "Qwen3_p150": {"seconds_per_gb": 12.5, "sample_count": 1},
    "Wan2.2_p300x2": {"seconds_per_gb": 5.5, "sample_count": 7}
  }
}
```

WAN 2.2 warm load is remarkably consistent: warmup phase is **~113–116s** (stddev <5s across 7 samples) — ideal for a reliable progress estimate. Device init is also highly consistent at **~14s**.

Cross-device tier ratios (used in cascade step 4): p150x4 ≈ 0.25× p150 load time (4 chips in parallel), p300x2 ≈ 0.25× p150 (same 4-die count as p150x4), p300 ≈ 0.5× p150. These are initial estimates; they self-correct once real samples accumulate.

---

## Threading Discipline

Identical to tt-local-generator: **GTK is single-threaded.** All background work runs in `threading.Thread`. All widget updates go through `GLib.idle_add(fn, *args)`. The `worker.py` module provides `idle_add` convenience wrappers. ServerManager and HealthWorker both follow this contract strictly.

---

## Patterns Carried Over from tt-local-generator

- Full Tenstorrent CSS palette and font stack
- `HealthWorker` background thread pattern (adapted for `/v1/models`)
- Auto-scroll-unless-scrolled-up logic for the log `Gtk.TextView`
- `GLib.idle_add` discipline throughout
- `app_settings.py` load/save pattern
- `gi.require_version` guards at import time
- CSS class names (`section-label`, `mock-*` not needed here but palette classes reused)
