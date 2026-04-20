# tt-model-runner-gui

A local desktop station for loading, configuring, launching, benchmarking, and
exploring AI models on Tenstorrent hardware. Ships two independent front-ends —
a full **GTK 4 GUI** and a **Textual TUI** — both driven by the same
`AppController` core with no UI code in the business logic.

```
./run            # GTK 4 windowed GUI
./run --tui      # Textual terminal UI (same controller, same features)
```

---

## Vision

Tenstorrent hardware is fast, but the gap between "I have a Quietbox" and
"I am running inference" is wide: find the right Docker image, configure
context length and concurrency, wait for trace capture, parse log output for
errors, run benchmarks. **tt-model-runner-gui bridges that gap** — one window
that knows about every supported model, what hardware it runs on, what it
expects at launch, and how to tell you when it is ready.

Out of scope: prompt submission, completions, evals, multi-host orchestration.
In scope: everything needed to go from powered-on hardware to a working
inference endpoint, repeatably.

---

## Features

### Model discovery
- Browse the full **tt-inference-server** model catalog (parsed from
  `model_spec.json`) with family grouping, type filter chips, and live search
- Fetch and cache the **Tenstorrent compatibility catalog**
  (`compatibility.json`, 222 models, refreshed every 24 h) — discover
  `tt-forge` and `tt-metal` models that are not in the inference-server
- **HF cache badges** next to every model name that is already downloaded
- **Architecture facts** strip (layer count, head count, context limit,
  vocabulary size, KV-cache estimate) read from the local HF cache
- **Compat hardware row** showing which Tenstorrent devices support the
  selected model and at what status (Supported / Experimental)
- **Model descriptions** sourced from a local JSON file and the compatibility
  catalog, with fallback ordering
- **Recently launched** section at the top of the rail for one-click re-launch
- **Starred models** that persist across sessions

### Launch
- **Full launch** through `tt-inference-server` — builds the Docker command
  from selected model, device, port, and options; streams stdout/stderr live
- **Developer image launch** for `tt-forge` and `tt-metal` models via a
  separate `tt-developer-image` container; buttons appear only when the model
  script is present on disk
- Launch **profiles** — save and restore named option sets (context length,
  tool use, dev mode, concurrency, etc.)
- **Use-case presets** (Chat, Code, Agent, Research, Creative, Dev, Smoke)
  that apply sensible defaults for each workflow
- **Port conflict detection** — socket + Docker check before launch; auto-
  redirects to reconnect if a TT container is already serving on that port
- **Estimated load time** from `TimingStore`, shown as a progress bar with
  seconds remaining during the LOADING state
- **Quick Settings** always visible: context length, max concurrent sequences,
  tool use, dev mode, timeout disable, SW validation skip, trace disable

### Server management
- State machine: IDLE → LAUNCHING → PULLING_IMAGE → LOADING → READY (or
  RUNNING/DONE for dev-image mode, ERROR on failure)
- **Running server detection** on startup — scans `docker ps` and surfaces a
  reconnect banner if a TT inference container is already running
- **Auto-identify model on reconnect** — calls `/v1/models` and matches
  against the catalog; both UIs auto-select the matched model
- **Stop detected server** without reconnecting (kills the container)
- **Restart** replays the last launch without re-pulling the Docker image
- **Uptime counter** ticking in the READY banner
- **Copy curl** command to clipboard (one-click test against the live endpoint)
- **Open API docs** in the system browser (`/docs` endpoint)

### Logs
- Streaming log view with **DEBUG / INFO / WARN / ERROR** level filter buttons
- **Jump to last error** button that appears on the first ERROR line
- **Log search** with regex, level filters, and match navigation
- **Save log** to disk via a file-picker dialog
- **Copy log** (all visible lines) to clipboard
- **Session log persistence** — every session is written to
  `~/.local/share/tt-runner-gui/logs/session-YYYY-MM-DD.log` and can be
  reloaded in a later session

### Hardware
- **Live chip telemetry** from `tt-smi -s` — per-chip temperature, clock
  speed, firmware version, and PCIe link status shown in the sidebar
- **Hardware reset** (`tt-smi -r`) with confirmation dialog; warns when
  switching between incompatible inference engines
- Port availability dot (green/red, debounced socket check)

### Benchmarking
- Wraps `tt-inference-server/run.py --workflow benchmarks`
- Modes: smoke-test, ci-nightly, ci-long; optional concurrency sweeps and
  percentile report
- Live output streamed into the bench log view
- Results table with pass/fail evaluation against `model_spec.json` targets
  (TTFT, TPS, throughput, end-to-end latency)
- **Aggregate stats** row (min/max/mean across runs)
- Export results to CSV

### Tool-call testing
- Multi-turn HTTP session against `/v1/chat/completions` with tool definitions
- Displays each request/response round-trip inline
- Shows a hint when tool use was not enabled at launch

### Model downloads
- **Download to HF cache** button — uses `huggingface_hub.snapshot_download`
  with a progress bar; falls back to `huggingface-cli`
- In-app **HF token** entry (masked, persisted to `~/.huggingface/token` and
  app settings); read from environment, token file, or settings on startup

### Docker image management
- List local TT Docker images with size and creation date
- Pull images from GHCR
- Prune dangling images

### Server repo management
- Configurable path to a local `tt-inference-server` checkout
- **git pull** with live output streamed to the log view; git branch and short
  SHA shown below the repo entry after pull

### Rotating "Did you know?" panel
- 36 static educational cards covering Tensix architecture, TT Metal internals,
  inference techniques, and workflow tips
- Dynamic model-recommendation cards based on detected hardware
- Auto-advances every 8 s; click to pause or skip; click a model card to jump
  to it in the rail

---

## Dependencies

### Runtime

| Package | Why |
|---------|-----|
| `PyGObject` / GTK 4 | GTK 4 GUI front-end (system package, not pip) |
| `textual >= 0.61` | Terminal UI front-end |
| `httpx >= 0.27` | Health polling and tool-call HTTP client |
| `requests >= 2.31` | HF Hub download fallback |
| `huggingface_hub` | `snapshot_download` for model weights (optional; falls back to CLI) |

GTK 4 and the `gi` Python bindings must be installed via the system package
manager (`apt install python3-gi gir1.2-gtk-4.0`). They are not pip-
installable; the virtual environment should be created with
`--system-site-packages` so `gi` is visible.

External tools used at runtime (not pip deps):

| Tool | Used for |
|------|----------|
| `docker` | Launch/stop inference containers |
| `tt-smi` | Hardware telemetry and device reset |
| `git` | Server repo pull and branch/SHA display |
| `huggingface-cli` | Download fallback when `huggingface_hub` is absent |

### Dev / test

```
pytest
respx >= 0.21   # HTTP mock for tool-client tests
```

---

## Quick start

```bash
# Clone and set up (system-site-packages for GTK gi bindings)
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt

# Launch the GUI
./run

# Launch the TUI
./run --tui

# Run tests
PYTHONPATH=app pytest tests/ -v
```

The app reads `model_spec.json` from the configured `tt-inference-server`
checkout (`~/.config/tt-runner-gui/settings.json` → `server_repo_path`).
On first run, set the repo path in the sidebar SERVER REPO field.

Settings are persisted to `~/.config/tt-runner-gui/settings.json`.
Logs are written to `~/.config/tt-runner-gui/app.log` (rotating, 2 MB).

---

## Architecture

```
AppController (controller.py)
  ├── ServerManager     — spawns Docker process, tails stdout
  ├── HealthWorker      — polls /v1/models or /tt-liveness
  ├── BenchmarkRunner   — wraps run.py --workflow benchmarks
  ├── ToolClient        — multi-turn tool-call HTTP session
  ├── TimingStore       — warm-start load-time estimates
  ├── DevImageLauncher  — tt-developer-image container launch
  └── CompatCatalog     — 222-model compatibility.json (async, 24 h cache)

Views (thin — only call controller methods and register on_* callbacks)
  ├── main_window.py    — GTK 4 (Sidebar + MainPanel + ConfigPanel)
  └── tui/app.py        — Textual (ModelRail + LogPane + ConfigPane +
                          ToolPane + BenchPane + ImagesPane)
```

All `on_*` callbacks are dispatched through a `dispatch_fn` injected at
construction time (`GLib.idle_add` for GTK, `app.call_from_thread` for
Textual, `lambda fn, *a: fn(*a)` for tests). No widget method is ever called
from a background thread.

---

## TUI key bindings

| Key | Action |
|-----|--------|
| `L` | Launch / Stop server |
| `Q` | Quit |
| `1`–`5` | Switch tab (Config / Logs / Tools / Bench / Images) |
| `[` | Toggle model rail sidebar |
| `S` | Star / unstar selected model |
| `R` | Reconnect to detected running server |
| `Ctrl+R` | Restart server (same model, no re-pull) |
| `Ctrl+H` | Refresh chip telemetry |
| `Ctrl+T` ×2 | Hardware reset (two presses within 5 s) |
| `Ctrl+U` | Copy test curl command (READY only) |
| `Ctrl+B` | Open API docs in browser (READY only) |
| `Ctrl+G` | git pull server repo |
| `Ctrl+L` | Copy all visible log lines |
| `Ctrl+F` | Open log search |
| `Ctrl+P` | Load most recent previous session log |

---

## License

Apache-2.0
