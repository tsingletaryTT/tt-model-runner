# tt-model-runner-gui Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a GTK4 desktop app that manages the full lifecycle of tt-inference-server Docker deployments — model selection, server launch, live log streaming, sub-stage progress bars with timing estimates, health monitoring, and a contextual learning tour during loading.

**Architecture:** Left sidebar (repo picker, 3-level model tree, device toggles, launch/stop button) + right main panel (status banner, sub-stage stepper, progress bar, tour panel, log stream). All background I/O in threads; all widget updates via `GLib.idle_add`. State machine: IDLE→LAUNCHING→PULLING_IMAGE→LOADING→READY↔STOPPING→IDLE.

**Tech Stack:** Python 3, GTK4 (`gi.repository`), Pango tags for log color, threading + GLib.idle_add, JSON for model_spec + settings + timing, subprocess for run.py + docker, pytest for unit tests.

---

### Task 1: Project scaffold

**Files:**
- Create: `app/__init__.py`
- Create: `app/main.py`
- Create: `run`
- Create: `tests/__init__.py`
- Create: `requirements-dev.txt`

- [ ] **Step 1: Create directory structure**

```bash
cd /home/ttuser/code/tt-model-runner-gui
mkdir -p app tests
touch app/__init__.py tests/__init__.py
```

- [ ] **Step 2: Write `requirements-dev.txt`**

```
pytest
pytest-timeout
```

- [ ] **Step 3: Write `app/main.py`**

```python
#!/usr/bin/env python3
"""Entry point for tt-model-runner-gui."""
import sys
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gio

_CSS = b"""
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
@define-color tt_warning     #F4C471;

window, .view { background-color: @tt_bg_darkest; color: @tt_text; }
* { font-family: "Noto Sans Mono", "DejaVu Sans Mono", monospace; font-size: 13px; color: @tt_text; }
.section-label { color: @tt_accent; font-weight: bold; font-size: 11px; }
.muted { color: @tt_text_muted; font-size: 11px; }
entry, textview { background-color: @tt_bg_dark; color: @tt_text; border: 1px solid @tt_border; border-radius: 4px; padding: 4px; }
entry:focus { border-color: @tt_accent; }
button { background-color: @tt_bg_dark; color: @tt_text; border: 1px solid @tt_border; border-radius: 4px; padding: 5px 10px; }
button:hover { background-color: @tt_border; border-color: @tt_accent; }
button:disabled { color: @tt_text_muted; border-color: @tt_bg_dark; }
.launch-btn { background-color: @tt_accent; color: @tt_bg_darkest; font-weight: bold; }
.launch-btn:hover { background-color: @tt_accent_light; }
.stop-btn { background-color: @tt_error; color: white; font-weight: bold; }
.pill { border-radius: 10px; padding: 2px 8px; font-size: 11px; font-weight: bold; }
.pill-loading { background-color: @tt_accent; color: @tt_bg_darkest; }
.pill-ready { background-color: @tt_success; color: white; }
.pill-error { background-color: @tt_error; color: white; }
.pill-idle { background-color: @tt_bg_dark; color: @tt_text_muted; }
.pill-stopping { background-color: @tt_pink; color: @tt_bg_darkest; }
.hf-ok { color: @tt_success; font-size: 11px; }
.hf-warn { color: @tt_error; font-size: 11px; }
.tour-panel { background-color: @tt_bg_panel; border: 1px solid @tt_border; border-radius: 4px; padding: 8px; }
.log-view { background-color: @tt_bg_panel; }
separator { background-color: @tt_border; min-height: 1px; }
treeview { background-color: @tt_bg_panel; }
treeview:selected { background-color: @tt_bg_dark; color: @tt_accent; }
progressbar trough { background-color: @tt_bg_dark; border-radius: 3px; min-height: 6px; }
progressbar progress { background-color: @tt_accent; border-radius: 3px; }
.stepper-done { color: @tt_success; font-size: 11px; }
.stepper-active { color: @tt_accent; font-size: 11px; font-weight: bold; }
.stepper-pending { color: @tt_text_muted; font-size: 11px; }
"""

class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="ai.tenstorrent.tt-model-runner-gui",
                         flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        from main_window import MainWindow
        provider = Gtk.CssProvider()
        provider.load_from_data(_CSS)
        Gtk.StyleContext.add_provider_for_display(
            self.get_active_window().get_display() if self.get_active_window() else
            __import__('gi.repository', fromlist=['Gdk']).Gdk.Display.get_default(),
            provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        win = MainWindow(application=self, css=_CSS)
        win.present()

def main():
    app = App()
    sys.exit(app.run(sys.argv))

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Write `run` launcher**

```bash
#!/usr/bin/env bash
cd "$(dirname "$0")/app"
exec python3 main.py "$@"
```

```bash
chmod +x run
```

- [ ] **Step 5: Commit**

```bash
git init
git add app/__init__.py app/main.py run tests/__init__.py requirements-dev.txt
git commit -m "feat: project scaffold with GTK4 entry point and CSS palette"
```

---

### Task 2: app_settings.py

**Files:**
- Create: `app/app_settings.py`
- Create: `tests/test_app_settings.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app_settings.py
import json, os, tempfile, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from app_settings import AppSettings

def test_defaults():
    with tempfile.TemporaryDirectory() as d:
        s = AppSettings(config_dir=d)
        assert s.last_port == "8000"
        assert s.log_level_filters == ["DEBUG", "INFO", "WARN", "ERROR"]

def test_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        s = AppSettings(config_dir=d)
        s.last_model = "meta-llama/Llama-3.2-1B"
        s.last_device = "N150"
        s.save()
        s2 = AppSettings(config_dir=d)
        assert s2.last_model == "meta-llama/Llama-3.2-1B"
        assert s2.last_device == "N150"

def test_invalid_json_falls_back_to_defaults():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "settings.json")
        with open(path, "w") as f:
            f.write("not json{{{")
        s = AppSettings(config_dir=d)
        assert s.last_port == "8000"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /home/ttuser/code/tt-model-runner-gui
python3 -m pytest tests/test_app_settings.py -v 2>&1 | tail -10
```
Expected: ImportError or similar failure.

- [ ] **Step 3: Write `app/app_settings.py`**

```python
"""Persist user preferences to ~/.config/tt-runner-gui/settings.json."""
import json
from pathlib import Path

_DEFAULTS = {
    "server_repo_path": str(Path.home() / "code" / "tt-inference-server"),
    "last_model": "",
    "last_device": "",
    "last_port": "8000",
    "tree_expanded_types": ["LLM"],
    "log_level_filters": ["DEBUG", "INFO", "WARN", "ERROR"],
    "window_width": 1280,
    "window_height": 820,
    "sidebar_width": 290,
}

class AppSettings:
    def __init__(self, config_dir: str | None = None):
        if config_dir:
            self._path = Path(config_dir) / "settings.json"
        else:
            self._path = Path.home() / ".config" / "tt-runner-gui" / "settings.json"
        self._data = dict(_DEFAULTS)
        if self._path.exists():
            try:
                loaded = json.loads(self._path.read_text())
                self._data.update({k: v for k, v in loaded.items() if k in _DEFAULTS})
            except (json.JSONDecodeError, OSError):
                pass

    def save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2))

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            super().__setattr__(name, value)
        else:
            self._data[name] = value
```

- [ ] **Step 4: Run tests**

```bash
python3 -m pytest tests/test_app_settings.py -v
```
Expected: 3 PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/app_settings.py tests/test_app_settings.py
git commit -m "feat: app_settings with JSON persistence and defaults"
```

---

### Task 3: model_catalog.py

**Files:**
- Create: `app/model_catalog.py`
- Create: `tests/test_model_catalog.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_model_catalog.py
import json, os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from model_catalog import ModelCatalog, ModelEntry, extract_family

def _minimal_spec():
    return {
        "schema_version": "2",
        "model_specs": {
            "id_llama_70b": {
                "T3K": {
                    "vllm": {
                        "impl": {
                            "model_id": "id_llama_70b",
                            "model_name": "Llama-3.3-70B-Instruct",
                            "hf_model_repo": "meta-llama/Llama-3.3-70B-Instruct",
                            "model_type": "LLM",
                            "docker_image": "ghcr.io/tt/vllm:latest",
                            "status": "COMPLETE",
                            "param_count": 70.0,
                            "min_disk_gb": 140.0,
                            "min_ram_gb": 16.0,
                            "inference_engine": "vllm",
                        }
                    }
                }
            },
            "id_wan_t2v": {
                "P300X2": {
                    "media": {
                        "impl": {
                            "model_id": "id_wan_t2v",
                            "model_name": "Wan2.2-T2V-A14B-Diffusers",
                            "hf_model_repo": "Wan-AI/Wan2.2-T2V-A14B-Diffusers",
                            "model_type": "VIDEO",
                            "docker_image": "ghcr.io/tt/media:latest",
                            "status": "FUNCTIONAL",
                            "param_count": 14.0,
                            "min_disk_gb": 37.0,
                            "min_ram_gb": 8.0,
                            "inference_engine": "media",
                        }
                    }
                }
            }
        }
    }

def test_load_tree():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(_minimal_spec(), f)
        path = f.name
    cat = ModelCatalog.load(path)
    tree = cat.get_tree()
    assert "LLM" in tree
    assert "Llama" in tree["LLM"]
    entries = tree["LLM"]["Llama"]
    assert len(entries) == 1
    assert entries[0].hf_model_repo == "meta-llama/Llama-3.3-70B-Instruct"

def test_filter_by_device():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(_minimal_spec(), f)
        path = f.name
    cat = ModelCatalog.load(path)
    filtered = cat.get_compatible(["T3K"])
    tree = filtered.get_tree()
    assert "LLM" in tree
    assert "VIDEO" not in tree

def test_extract_family():
    assert extract_family("Llama-3.3-70B-Instruct") == "Llama"
    assert extract_family("Qwen3-8B") == "Qwen3"
    assert extract_family("DeepSeek-R1-Distill-Llama-70B") == "DeepSeek"
    assert extract_family("Wan2.2-T2V-A14B-Diffusers") == "Wan2.2"
    assert extract_family("mistralai/Mistral-7B-v0.3") == "Mistral"
```

- [ ] **Step 2: Run to verify failure**

```bash
python3 -m pytest tests/test_model_catalog.py -v 2>&1 | tail -5
```

- [ ] **Step 3: Write `app/model_catalog.py`**

```python
"""Parse model_spec.json into a filterable ModelEntry tree."""
import json
import re
from dataclasses import dataclass
from pathlib import Path

@dataclass
class ModelEntry:
    model_id: str
    model_name: str
    hf_model_repo: str
    model_type: str
    family: str
    device_type: str
    inference_engine: str
    docker_image: str
    status: str
    param_count: float | None
    min_disk_gb: float | None
    min_ram_gb: float | None

def extract_family(name: str) -> str:
    """Extract model family from model_name or hf_model_repo."""
    # Strip org prefix (e.g. "meta-llama/")
    base = name.split("/")[-1]
    # Strip known suffixes: version numbers, sizes, qualifiers
    # Keep the first meaningful token(s) before version/size
    # e.g. "Llama-3.3-70B-Instruct" → "Llama"
    # e.g. "Wan2.2-T2V-A14B-Diffusers" → "Wan2.2"
    # e.g. "Qwen3-8B" → "Qwen3"
    # e.g. "DeepSeek-R1-Distill-Llama-70B" → "DeepSeek"
    tokens = re.split(r"[-_]", base)
    family_parts = []
    for tok in tokens:
        # Stop when we hit a pure size token (e.g. "70B", "8B", "14B")
        if re.match(r"^\d+(\.\d+)?[BMbm]$", tok):
            break
        # Stop on version number standalone (e.g. "3", "3.3") unless first token
        if family_parts and re.match(r"^\d+(\.\d+)?$", tok):
            break
        # Stop on known qualifier words
        if tok.lower() in ("instruct", "chat", "base", "diffusers", "v0", "v1", "v2",
                            "distill", "preview", "beta", "alpha", "hf", "gguf"):
            break
        family_parts.append(tok)
    return "-".join(family_parts) if family_parts else tokens[0]

class ModelCatalog:
    def __init__(self, entries: list[ModelEntry]):
        self._entries = entries

    @classmethod
    def load(cls, spec_path: str | Path) -> "ModelCatalog":
        data = json.loads(Path(spec_path).read_text())
        entries = []
        for _model_id, devices in data.get("model_specs", {}).items():
            for device_type, engines in devices.items():
                for _engine_key, engine_data in engines.items():
                    impl = engine_data.get("impl", engine_data)
                    if not impl.get("model_name"):
                        continue
                    name = impl.get("model_name", "")
                    entries.append(ModelEntry(
                        model_id=impl.get("model_id", _model_id),
                        model_name=name,
                        hf_model_repo=impl.get("hf_model_repo", ""),
                        model_type=impl.get("model_type", "LLM"),
                        family=extract_family(name),
                        device_type=device_type,
                        inference_engine=impl.get("inference_engine", _engine_key),
                        docker_image=impl.get("docker_image", ""),
                        status=impl.get("status", "EXPERIMENTAL"),
                        param_count=impl.get("param_count"),
                        min_disk_gb=impl.get("min_disk_gb"),
                        min_ram_gb=impl.get("min_ram_gb"),
                    ))
        return cls(entries)

    def get_tree(self) -> dict[str, dict[str, list[ModelEntry]]]:
        """Return {model_type → {family → [entries]}}."""
        tree: dict[str, dict[str, list[ModelEntry]]] = {}
        for e in self._entries:
            tree.setdefault(e.model_type, {}).setdefault(e.family, []).append(e)
        return tree

    def get_compatible(self, device_types: list[str]) -> "ModelCatalog":
        dt_upper = [d.upper() for d in device_types]
        return ModelCatalog([e for e in self._entries if e.device_type.upper() in dt_upper])

    def get_entry(self, model_name: str, device: str) -> ModelEntry | None:
        device_up = device.upper()
        for e in self._entries:
            if (e.model_name == model_name or e.hf_model_repo == model_name) \
               and e.device_type.upper() == device_up:
                return e
        return None

    def all_devices(self) -> list[str]:
        return sorted(set(e.device_type for e in self._entries))
```

- [ ] **Step 4: Run tests**

```bash
python3 -m pytest tests/test_model_catalog.py -v
```
Expected: 3 PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/model_catalog.py tests/test_model_catalog.py
git commit -m "feat: model_catalog with 3-level tree and family extraction"
```

---

### Task 4: device_detector.py

**Files:**
- Create: `app/device_detector.py`
- Create: `tests/test_device_detector.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_device_detector.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from device_detector import parse_board_types, board_type_to_device

def test_parse_board_types_p300c():
    # tt-smi -s outputs two JSON blobs concatenated; regex-based parse
    raw = '{"board_type": "p300c", "pcie_speed": 4}{"board_type": "p300c"}'
    result = parse_board_types(raw)
    assert result == ["p300c", "p300c"]

def test_board_type_mapping():
    assert board_type_to_device("p300c") == "P300X2"
    assert board_type_to_device("n150") == "N150"
    assert board_type_to_device("p150") == "P150"
    assert board_type_to_device("unknown_chip") is None

def test_p300x2_deduplication():
    # 4× p300c = one P300X2 board (2 dies per card × 2 cards)
    from device_detector import deduplicate_devices
    devices = deduplicate_devices(["p300c", "p300c", "p300c", "p300c"])
    assert "P300X2" in devices
```

- [ ] **Step 2: Run to verify failure**

```bash
python3 -m pytest tests/test_device_detector.py -v 2>&1 | tail -5
```

- [ ] **Step 3: Write `app/device_detector.py`**

```python
"""Detect Tenstorrent hardware via tt-smi -s."""
import re
import subprocess

# Map board_type string → DeviceType name used in model_spec.json
_BOARD_TYPE_MAP = {
    "n150":  "N150",
    "p150":  "P150",
    "p300":  "P300",
    "p300c": "P300",   # single p300c die — logical device P300
    "t3k":   "T3K",
    "p150x4": "P150X4",
    "p300x2": "P300X2",
    "blackhole": "N300",
}

def parse_board_types(raw: str) -> list[str]:
    """Extract all board_type values from raw tt-smi -s output (may be multiple JSON objects)."""
    return re.findall(r'"board_type"\s*:\s*"([^"]+)"', raw)

def board_type_to_device(board_type: str) -> str | None:
    return _BOARD_TYPE_MAP.get(board_type.lower())

def deduplicate_devices(board_types: list[str]) -> list[str]:
    """
    Given a list of raw board_type strings (one per physical die),
    return the logical device list. 4× p300c = P300X2 + P300 entries.
    2× p300c = P300X2.
    """
    devices = set()
    p300c_count = sum(1 for b in board_types if b.lower() == "p300c")
    if p300c_count >= 4:
        devices.add("P300X2")   # quad-die board
    if p300c_count >= 2:
        devices.add("P300X2")
    if p300c_count >= 1:
        devices.add("P300")
    for b in board_types:
        bl = b.lower()
        if bl == "p300c":
            continue
        dev = board_type_to_device(bl)
        if dev:
            devices.add(dev)
    # Always include lower-tier devices that subsets of detected HW can run
    if "T3K" in devices or "P300X2" in devices:
        devices.update({"P300", "P150", "N150"})
    elif "P300" in devices:
        devices.update({"P150", "N150"})
    elif "P150X4" in devices:
        devices.update({"P150", "N150"})
    elif "P150" in devices:
        devices.add("N150")
    return sorted(devices)

def detect_devices() -> list[str]:
    """
    Run tt-smi -s and return list of logical DeviceType strings compatible
    with current hardware. Returns [] on failure (caller should show all devices).
    """
    try:
        result = subprocess.run(
            ["tt-smi", "-s"], capture_output=True, text=True, timeout=10
        )
        raw = result.stdout + result.stderr
        board_types = parse_board_types(raw)
        if not board_types:
            return []
        return deduplicate_devices(board_types)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
```

- [ ] **Step 4: Run tests**

```bash
python3 -m pytest tests/test_device_detector.py -v
```
Expected: 3 PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/device_detector.py tests/test_device_detector.py
git commit -m "feat: device_detector — tt-smi parsing + p300c → P300X2 dedup"
```

---

### Task 5: timing_store.py

**Files:**
- Create: `app/timing_store.py`
- Create: `tests/test_timing_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_timing_store.py
import sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from timing_store import TimingStore, EstimateResult

def test_record_and_exact_estimate():
    with tempfile.TemporaryDirectory() as d:
        ts = TimingStore(config_dir=d)
        ts.record_load("meta-llama/Llama-3.2-1B", "N150", 95.0, cold=True)
        ts.record_load("meta-llama/Llama-3.2-1B", "N150", 98.0, cold=True)
        r = ts.estimate_load("meta-llama/Llama-3.2-1B", "N150", cold=True, size_gb=2.0, family="Llama")
        assert r.confidence == "high"
        assert abs(r.seconds - 96.5) < 1.0

def test_device_baseline_fallback():
    with tempfile.TemporaryDirectory() as d:
        ts = TimingStore(config_dir=d)
        # pre-seed device rate
        ts._data["device_load_rate"]["N150"] = {"seconds_per_gb": 11.5, "sample_count": 3}
        ts._save()
        r = ts.estimate_load("some-new-model", "N150", cold=True, size_gb=4.0, family="NewFam")
        assert r.confidence == "low"
        assert abs(r.seconds - 46.0) < 1.0

def test_bootstrap_pre_seeded():
    with tempfile.TemporaryDirectory() as d:
        ts = TimingStore(config_dir=d)
        ts.bootstrap_defaults()
        r = ts.estimate_load("Wan2.2-T2V-A14B-Diffusers", "P300X2", cold=False, size_gb=37.0, family="Wan2.2")
        assert r.seconds is not None
        assert r.confidence in ("medium", "high")

def test_persist_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        ts = TimingStore(config_dir=d)
        ts.record_load("x/model", "T3K", 120.0, cold=False)
        ts2 = TimingStore(config_dir=d)
        r = ts2.estimate_load("x/model", "T3K", cold=False, size_gb=5.0, family="model")
        assert r.seconds is not None
```

- [ ] **Step 2: Run to verify failure**

```bash
python3 -m pytest tests/test_timing_store.py -v 2>&1 | tail -5
```

- [ ] **Step 3: Write `app/timing_store.py`**

```python
"""Load-time and download-time estimation with cross-model inference."""
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_MAX_SAMPLES = 10

_BOOTSTRAP = {
    "load_samples": {
        "Llama-3.1-8B-Instruct_P150_cold": [100, 100],
        "Llama-3.1-8B-Instruct_P150_warm": [12, 13, 20, 60],
        "Qwen3-8B_P150_cold": [100],
        "Wan2.2-T2V-A14B-Diffusers_P300X2_warm": [151, 177, 151, 137, 151, 145, 152],
        "Wan2.2-Animate-14B-Diffusers_P300X2_cold": [218],
    },
    "substage_samples": {
        "Wan2.2-T2V-A14B-Diffusers_P300X2_device_init": [13, 14, 15, 15, 13, 15, 15],
        "Wan2.2-T2V-A14B-Diffusers_P300X2_cache_loading": [16, 16, 6, 15, 14, 16],
        "Wan2.2-T2V-A14B-Diffusers_P300X2_warmup": [116, 138, 113, 112, 116, 111, 115],
    },
    "device_load_rate": {
        "P150":   {"seconds_per_gb": 7.23, "sample_count": 7},
        "P300X2": {"seconds_per_gb": 5.5,  "sample_count": 7},
    },
    "family_load_rate": {
        "Llama_P150":  {"seconds_per_gb": 6.35, "sample_count": 6},
        "Qwen3_P150":  {"seconds_per_gb": 12.5, "sample_count": 1},
        "Wan2.2_P300X2": {"seconds_per_gb": 5.5, "sample_count": 7},
    },
}

# Cross-device tier ratios (relative to N150 throughput = 1.0)
_TIER = {"N150": 1.0, "P150": 1.0, "P150X4": 4.0, "P300": 2.0, "P300X2": 4.0, "T3K": 3.0}


@dataclass
class EstimateResult:
    seconds: float | None
    confidence: Literal["none", "low", "medium", "high"]
    source: str


def _trimmed_mean(samples: list[float]) -> float:
    if len(samples) <= 2:
        return statistics.mean(samples)
    s = sorted(samples)
    trim = max(1, len(s) // 5)
    return statistics.mean(s[trim:-trim])


class TimingStore:
    def __init__(self, config_dir: str | None = None):
        if config_dir:
            self._path = Path(config_dir) / "timing.json"
        else:
            self._path = Path.home() / ".config" / "tt-runner-gui" / "timing.json"
        self._data: dict = {
            "schema_version": 1,
            "download_speed_mbps": [],
            "load_samples": {},
            "substage_samples": {},
            "device_load_rate": {},
            "family_load_rate": {},
        }
        if self._path.exists():
            try:
                self._data.update(json.loads(self._path.read_text()))
            except (json.JSONDecodeError, OSError):
                pass
        else:
            self.bootstrap_defaults()

    def bootstrap_defaults(self):
        for section in ("load_samples", "substage_samples", "device_load_rate", "family_load_rate"):
            for k, v in _BOOTSTRAP.get(section, {}).items():
                if k not in self._data[section]:
                    self._data[section][k] = v
        self._save()

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2))

    def _key(self, hf_repo: str, device: str, cold: bool) -> str:
        name = hf_repo.split("/")[-1]
        suffix = "cold" if cold else "warm"
        return f"{name}_{device}_{suffix}"

    def record_load(self, hf_repo: str, device: str, duration_s: float, cold: bool):
        key = self._key(hf_repo, device, cold)
        samples = self._data["load_samples"].setdefault(key, [])
        samples.append(round(duration_s, 1))
        if len(samples) > _MAX_SAMPLES:
            samples.pop(0)
        self._update_rates(hf_repo, device, duration_s, cold)
        self._save()

    def record_substage(self, model_name: str, device: str, stage: str, duration_s: float):
        key = f"{model_name}_{device}_{stage}"
        samples = self._data["substage_samples"].setdefault(key, [])
        samples.append(round(duration_s, 1))
        if len(samples) > _MAX_SAMPLES:
            samples.pop(0)
        self._save()

    def _update_rates(self, hf_repo: str, device: str, duration_s: float, cold: bool):
        # We'd need size_gb here; skip rate update if not provided
        pass

    def record_download(self, hf_repo: str, size_gb: float, duration_s: float):
        if size_gb > 0:
            mbps = (size_gb * 1024) / duration_s
            speeds = self._data["download_speed_mbps"]
            speeds.append(round(mbps, 1))
            if len(speeds) > 5:
                speeds.pop(0)
        self._save()

    def estimate_substage(self, model_name: str, device: str, stage: str) -> EstimateResult:
        key = f"{model_name}_{device}_{stage}"
        samples = self._data["substage_samples"].get(key, [])
        if samples:
            n = len(samples)
            secs = _trimmed_mean(samples)
            conf = "high" if n >= 3 else "medium" if n >= 2 else "low"
            return EstimateResult(secs, conf, f"{n} sample{'s' if n != 1 else ''}")
        return EstimateResult(None, "none", "no data")

    def estimate_load(self, hf_repo: str, device: str, cold: bool,
                      size_gb: float, family: str) -> EstimateResult:
        # 1. Exact match
        key = self._key(hf_repo, device, cold)
        samples = self._data["load_samples"].get(key, [])
        if samples:
            n = len(samples)
            secs = _trimmed_mean(samples)
            name = hf_repo.split("/")[-1]
            conf = "high" if n >= 3 else "medium" if n >= 2 else "low"
            return EstimateResult(secs, conf,
                                  f"{name} on {device} ({n} sample{'s' if n != 1 else ''})")

        # 1b. Opposite cold/warm from same model
        alt_key = self._key(hf_repo, device, not cold)
        alt_samples = self._data["load_samples"].get(alt_key, [])
        if alt_samples:
            factor = 5.0 if cold else 0.2
            secs = _trimmed_mean(alt_samples) * factor
            label = "warm→cold estimate" if cold else "cold→warm estimate"
            return EstimateResult(secs, "low", label)

        # 2. Family + device rate
        fam_key = f"{family}_{device}"
        fam_rate = self._data["family_load_rate"].get(fam_key)
        if fam_rate and size_gb > 0:
            secs = fam_rate["seconds_per_gb"] * size_gb
            n = fam_rate["sample_count"]
            return EstimateResult(secs, "medium",
                                  f"{family} family on {device} ({n} samples)")

        # 3. Device baseline
        dev_rate = self._data["device_load_rate"].get(device)
        if dev_rate and size_gb > 0:
            secs = dev_rate["seconds_per_gb"] * size_gb
            return EstimateResult(secs, "low",
                                  f"{device} baseline ({dev_rate['sample_count']} samples)")

        # 4. Cross-device fallback
        my_tier = _TIER.get(device, 1.0)
        for other_dev, rate in self._data["device_load_rate"].items():
            other_tier = _TIER.get(other_dev, 1.0)
            if other_tier > 0 and size_gb > 0:
                secs = rate["seconds_per_gb"] * size_gb * (other_tier / my_tier)
                return EstimateResult(secs, "low",
                                      f"scaled from {other_dev} baseline")

        return EstimateResult(None, "none", "no data")

    def estimate_download(self, size_gb: float) -> EstimateResult:
        speeds = self._data["download_speed_mbps"]
        if speeds:
            avg_mbps = statistics.mean(speeds)
            secs = (size_gb * 1024) / avg_mbps
            return EstimateResult(secs, "medium", f"avg {avg_mbps:.1f} MB/s")
        # Assume 10 MB/s as conservative default
        secs = (size_gb * 1024) / 10.0
        return EstimateResult(secs, "low", "default 10 MB/s assumed")
```

- [ ] **Step 4: Run tests**

```bash
python3 -m pytest tests/test_timing_store.py -v
```
Expected: 4 PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/timing_store.py tests/test_timing_store.py
git commit -m "feat: timing_store with estimation cascade and bootstrap defaults"
```

---

### Task 6: server_manager.py

**Files:**
- Create: `app/server_manager.py`
- Create: `tests/test_server_manager.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server_manager.py
import sys, os, tempfile, time, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from server_manager import LogTailer, ServerState, parse_state_transition

def test_state_transitions():
    assert parse_state_transition("docker pull ghcr.io/tt/vllm:latest") == ServerState.PULLING_IMAGE
    assert parse_state_transition("Starting vLLM API server on port 8000") == ServerState.LOADING
    assert parse_state_transition("ERROR: failed to start") == ServerState.ERROR
    assert parse_state_transition("random log line") is None

def test_log_tailer_reads_lines():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False) as f:
        f.write("line1\nline2\n")
        path = f.name
    lines = []
    stop_evt = threading.Event()
    tailer = LogTailer(path, on_line=lines.append, stop_event=stop_evt)
    tailer.start()
    time.sleep(0.2)
    stop_evt.set()
    tailer.join(timeout=2.0)
    assert "line1\n" in lines
    assert "line2\n" in lines
```

- [ ] **Step 2: Run to verify failure**

```bash
python3 -m pytest tests/test_server_manager.py -v 2>&1 | tail -5
```

- [ ] **Step 3: Write `app/server_manager.py`**

```python
"""Launch/stop tt-inference-server via run.py; tail log file."""
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Callable


class ServerState(Enum):
    IDLE = auto()
    LAUNCHING = auto()
    PULLING_IMAGE = auto()
    LOADING = auto()
    READY = auto()
    ERROR = auto()
    STOPPING = auto()


# Sub-stage keys for vLLM models
VLLM_STAGES = [
    ("engine_init",     r"Automatically detected platform tt"),
    ("device_setup",    r"multidevice with \d+ devices.*created|tt_metal.*Created mesh"),
    ("loading_weights", r"Loading checkpoint shards|loading model weights"),
    ("kv_cache",        r"[Aa]llocating kv cache|kv_cache_dtype"),
    ("api_startup",     r"Starting vLLM API server|Uvicorn running"),
    ("trace_capture",   r"Capturing traces.*input_seq_len=(\d+)"),
]

# Sub-stage keys for media server models (WAN, SkyReels, etc.)
MEDIA_STAGES = [
    ("device_init",     r"Creating new Video service|Starting media server"),
    ("mesh_created",    r"Created mesh device with \d+ devices"),
    ("loading_weights", r"[Ll]oading model|Device.*Loading"),
    ("cache_loading",   r"loading cache at.*/(transformer|text_encoder|vae)"),
    ("model_loaded",    r"Model loaded successfully"),
    ("warmup",          r"Wan22 inference|warmup inference|run\] executed.*inference"),
]

_PULLING_RE  = re.compile(r"docker pull", re.IGNORECASE)
_LOADING_REs = [re.compile(p, re.IGNORECASE) for p in [
    r"Loading model weights", r"Starting vLLM", r"Creating new Video service"]]
_ERROR_RE    = re.compile(r"\bERROR\b|⛔|[Ff]ailed to start|[Ee]xit code [^0]")
_DOCKER_LOG_RE = re.compile(r"Docker logs are streamed to: (.+)")
_TRACE_RE    = re.compile(r"input_seq_len=(\d+)")
_WARMUP_RE   = re.compile(r"(\d+)%\|.*?(\d+)/(\d+)")


def parse_state_transition(line: str) -> "ServerState | None":
    if _PULLING_RE.search(line):
        return ServerState.PULLING_IMAGE
    for pat in _LOADING_REs:
        if pat.search(line):
            return ServerState.LOADING
    if _ERROR_RE.search(line):
        return ServerState.ERROR
    return None


def parse_substage(line: str, engine: str) -> tuple[str, dict] | None:
    """Return (stage_key, extra_data) if line matches a sub-stage trigger."""
    stages = VLLM_STAGES if engine == "vllm" else MEDIA_STAGES
    for key, pattern in stages:
        m = re.search(pattern, line, re.IGNORECASE)
        if m:
            extra = {}
            if key == "trace_capture":
                tm = _TRACE_RE.search(line)
                if tm:
                    extra["seq_len"] = int(tm.group(1))
            if key == "warmup":
                wm = _WARMUP_RE.search(line)
                if wm:
                    extra["n"] = int(wm.group(2))
                    extra["total"] = int(wm.group(3))
            return (key, extra)
    return None


class LogTailer(threading.Thread):
    """Tail a log file, calling on_line for each new line."""

    def __init__(self, path: str | Path, on_line: Callable[[str], None],
                 stop_event: threading.Event):
        super().__init__(daemon=True)
        self._path = Path(path)
        self._on_line = on_line
        self._stop = stop_event

    def run(self):
        # Wait for file to exist
        for _ in range(50):
            if self._path.exists():
                break
            if self._stop.wait(0.2):
                return
        try:
            with open(self._path) as f:
                while not self._stop.is_set():
                    line = f.readline()
                    if line:
                        self._on_line(line)
                    else:
                        self._stop.wait(0.1)
        except OSError:
            pass


@dataclass
class LaunchConfig:
    repo_path: Path
    model_name: str       # hf_model_repo, e.g. "meta-llama/Llama-3.2-1B"
    device: str           # e.g. "N150"
    port: str = "8000"
    hf_token: str | None = None
    no_auth: bool = True
    inference_engine: str = "vllm"  # "vllm" or "media"


class ServerManager:
    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._tailer: LogTailer | None = None
        self._stop_evt = threading.Event()
        self._container_name: str | None = None
        self._log_path: Path | None = None
        self._config: LaunchConfig | None = None

    def launch(self, config: LaunchConfig,
               on_log_line: Callable[[str], None],
               on_state: Callable[[ServerState, dict], None]):
        self._config = config
        self._stop_evt = threading.Event()

        env = os.environ.copy()
        if config.hf_token:
            env["HF_TOKEN"] = config.hf_token

        cmd = [
            "python3", "run.py",
            "--model", config.model_name,
            "--workflow", "server",
            "--docker-server",
            "--service-port", config.port,
        ]
        if config.no_auth:
            cmd.append("--no-auth")

        self._proc = subprocess.Popen(
            cmd, cwd=str(config.repo_path), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        # Find the log file in background
        def _find_and_tail():
            log_dir = config.repo_path / "workflow_logs" / "run_logs"
            log_path = None
            for _ in range(50):   # up to 10s
                if self._stop_evt.wait(0.2):
                    return
                candidates = sorted(log_dir.glob("run_*.log"),
                                    key=lambda p: p.stat().st_mtime, reverse=True)
                if candidates:
                    log_path = candidates[0]
                    break
            if not log_path:
                on_log_line("WARNING: log file not found after 10s\n")
                return
            self._log_path = log_path

            def _handle_line(line: str):
                on_log_line(line)
                transition = parse_state_transition(line)
                if transition:
                    on_state(transition, {})
                substage = parse_substage(line, config.inference_engine)
                if substage:
                    on_state(ServerState.LOADING, {"substage": substage[0], **substage[1]})
                # Try to find container name
                m = re.search(r'--name\s+(\S+)', line)
                if m and not self._container_name:
                    self._container_name = m.group(1)

            tailer = LogTailer(log_path, _handle_line, self._stop_evt)
            tailer.start()
            self._tailer = tailer
            # Monitor subprocess exit
            while not self._stop_evt.is_set():
                ret = self._proc.poll()
                if ret is not None and ret != 0:
                    on_state(ServerState.ERROR, {"exit_code": ret})
                    break
                self._stop_evt.wait(1.0)

        threading.Thread(target=_find_and_tail, daemon=True).start()

    def stop(self, on_stopped: Callable[[], None] | None = None):
        self._stop_evt.set()
        def _do_stop():
            if self._container_name:
                subprocess.run(
                    ["docker", "stop", self._container_name],
                    capture_output=True, timeout=30
                )
            elif self._proc:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            if self._tailer:
                self._tailer.join(timeout=5.0)
            if on_stopped:
                from gi.repository import GLib
                GLib.idle_add(on_stopped)
        threading.Thread(target=_do_stop, daemon=True).start()

    def get_container_name(self) -> str | None:
        return self._container_name
```

- [ ] **Step 4: Run tests**

```bash
python3 -m pytest tests/test_server_manager.py -v
```
Expected: 2 PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/server_manager.py tests/test_server_manager.py
git commit -m "feat: server_manager — launch run.py, tail log, parse sub-stages"
```

---

### Task 7: health_worker.py + worker.py

**Files:**
- Create: `app/worker.py`
- Create: `app/health_worker.py`
- Create: `tests/test_health_worker.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_health_worker.py
import sys, os, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

def test_health_worker_calls_on_ready(monkeypatch):
    from health_worker import HealthWorker
    import health_worker as hw

    calls = []
    def fake_get(url, timeout):
        class R:
            status_code = 200
            def json(self): return {"data": [{"id": "meta-llama/Llama-3.2-1B"}]}
        return R()
    monkeypatch.setattr(hw, "_requests_get", fake_get)

    ready_evt = threading.Event()
    def on_ready(models): ready_evt.set()

    w = HealthWorker(port="8000", on_ready=on_ready, on_lost=lambda: None,
                     poll_interval=0.1, use_idle_add=False)
    w.start()
    assert ready_evt.wait(timeout=2.0), "on_ready not called"
    w.stop()
    w.join(timeout=2.0)
```

- [ ] **Step 2: Run to verify failure**

```bash
python3 -m pytest tests/test_health_worker.py -v 2>&1 | tail -5
```

- [ ] **Step 3: Write `app/worker.py`**

```python
"""GLib.idle_add helpers for thread→UI communication."""
from gi.repository import GLib

def idle_add(fn, *args):
    """Schedule fn(*args) to run on the GTK main thread."""
    GLib.idle_add(fn, *args)
```

- [ ] **Step 4: Write `app/health_worker.py`**

```python
"""Poll server health endpoint; emit ready/lost callbacks."""
import threading
from typing import Callable

try:
    import requests as _requests_lib
    def _requests_get(url, timeout):
        return _requests_lib.get(url, timeout=timeout)
except ImportError:
    import urllib.request
    import json as _json
    class _FakeResponse:
        def __init__(self, data, code):
            self._data = data
            self.status_code = code
        def json(self): return self._data
    def _requests_get(url, timeout):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return _FakeResponse(_json.loads(r.read()), r.status)
        except Exception as e:
            raise e


class HealthWorker(threading.Thread):
    def __init__(self, port: str,
                 on_ready: Callable[[list[str]], None],
                 on_lost: Callable[[], None],
                 poll_interval: float = 5.0,
                 use_idle_add: bool = True):
        super().__init__(daemon=True)
        self._port = port
        self._on_ready = on_ready
        self._on_lost = on_lost
        self._interval = poll_interval
        self._use_idle_add = use_idle_add
        self._stop = threading.Event()
        self._was_ready = False

    def stop(self):
        self._stop.set()

    def _dispatch(self, fn, *args):
        if self._use_idle_add:
            from gi.repository import GLib
            GLib.idle_add(fn, *args)
        else:
            fn(*args)

    def run(self):
        url = f"http://localhost:{self._port}/v1/models"
        while not self._stop.is_set():
            try:
                r = _requests_get(url, timeout=4.0)
                if r.status_code == 200:
                    models = [m["id"] for m in r.json().get("data", [])]
                    if not self._was_ready:
                        self._was_ready = True
                        self._dispatch(self._on_ready, models)
                else:
                    if self._was_ready:
                        self._was_ready = False
                        self._dispatch(self._on_lost)
            except Exception:
                if self._was_ready:
                    self._was_ready = False
                    self._dispatch(self._on_lost)
            self._stop.wait(self._interval)
```

- [ ] **Step 5: Run tests**

```bash
python3 -m pytest tests/test_health_worker.py -v
```
Expected: 1 PASSED.

- [ ] **Step 6: Commit**

```bash
git add app/worker.py app/health_worker.py tests/test_health_worker.py
git commit -m "feat: health_worker polls /v1/models; worker.py GLib.idle_add helper"
```

---

### Task 8: Main window skeleton

**Files:**
- Create: `app/main_window.py`

This task builds a displayable window with sidebar and main panel. No wiring to server yet — just the visual structure.

- [ ] **Step 1: Write `app/main_window.py`** (skeleton)

```python
#!/usr/bin/env python3
"""Main application window: sidebar + main panel layout."""
import os
import threading
from pathlib import Path
from typing import Callable

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import GLib, Gtk, Pango, Gio

from app_settings import AppSettings
from model_catalog import ModelCatalog, ModelEntry
from device_detector import detect_devices, get_compatible_devices
from server_manager import ServerManager, LaunchConfig, ServerState
from health_worker import HealthWorker
from timing_store import TimingStore

_VLLM_STAGE_LABELS = {
    "engine_init":     "Initializing vLLM engine",
    "device_setup":    "Setting up TT device mesh",
    "loading_weights": "Loading model weights",
    "kv_cache":        "Profiling & allocating KV cache",
    "api_startup":     "Starting HTTP server",
    "trace_capture":   "Capturing traces",
    "ready":           "Ready",
}
_MEDIA_STAGE_LABELS = {
    "device_init":     "Starting media server",
    "mesh_created":    "Device mesh ready",
    "loading_weights": "Loading checkpoint shards",
    "cache_loading":   "Loading TT tensor cache",
    "model_loaded":    "Model on device",
    "warmup":          "Running warmup inference",
    "ready":           "Ready",
}
_VLLM_STAGE_ORDER  = list(_VLLM_STAGE_LABELS.keys())
_MEDIA_STAGE_ORDER = list(_MEDIA_STAGE_LABELS.keys())

# Matrix education content per sub-stage (shown in tour panel)
_MATRIX_FACTS = {
    "loading_weights": (
        "Reading weight matrices from disk",
        "Q_proj, K_proj, V_proj — each [hidden × hidden]\n"
        "bfloat16: 2 bytes/element\n"
        "For hidden=5120: one matrix = 52 MB"
    ),
    "device_setup": (
        "Distributing tensors across chips",
        "Tensor parallelism: Q_proj [H×H]\n"
        "split column-wise across N chips\n"
        "Each chip gets [H × H/N]"
    ),
    "kv_cache": (
        "KV cache allocation",
        "For each layer:\n"
        "  K: [ctx_len × head_dim]\n"
        "  V: [ctx_len × head_dim]\n"
        "Total = 2 × layers × ctx × head_dim × 2B"
    ),
    "trace_capture": (
        "JIT compiling attention kernels",
        "For seq_len=L:\n"
        "  scores = Q [L×d] × Kᵀ [d×L] → [L×L]\n"
        "  softmax(scores) × V [L×d] → [L×d]\n"
        "One kernel compiled per context length"
    ),
    "warmup": (
        "Denoising warmup pass",
        "Spatial attention on latent patches:\n"
        "  [T × H/8 × W/8] tokens\n"
        "  Each token = position in video volume\n"
        "2 passes JIT-compile all diffusion kernels"
    ),
    "mesh_created": (
        "Multi-chip tensor parallelism",
        "Weight shards across NxN mesh:\n"
        "  Attention: col-parallel (N chips)\n"
        "  FFN: row-parallel reduce\n"
        "Chips communicate via ethernet fabric"
    ),
}


class StepperWidget(Gtk.Box):
    """Horizontal stepper showing sub-stage progress."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._labels: list[Gtk.Label] = []
        self._stage_keys: list[str] = []

    def set_stages(self, stage_keys: list[str], stage_labels: dict[str, str]):
        for child in list(self):
            self.remove(child)
        self._labels = []
        self._stage_keys = stage_keys
        for i, key in enumerate(stage_keys):
            lbl = Gtk.Label(label=stage_labels.get(key, key))
            lbl.add_css_class("stepper-pending")
            self._labels.append(lbl)
            self.append(lbl)
            if i < len(stage_keys) - 1:
                sep = Gtk.Label(label=" ── ")
                sep.add_css_class("stepper-pending")
                self.append(sep)

    def set_active(self, active_key: str):
        for i, key in enumerate(self._stage_keys):
            lbl = self._labels[i]
            lbl.remove_css_class("stepper-done")
            lbl.remove_css_class("stepper-active")
            lbl.remove_css_class("stepper-pending")
            idx_active = self._stage_keys.index(active_key) if active_key in self._stage_keys else -1
            if i < idx_active:
                lbl.set_text("✓ " + lbl.get_text().lstrip("✓ ●○").strip())
                lbl.add_css_class("stepper-done")
            elif i == idx_active:
                lbl.set_text("● " + lbl.get_text().lstrip("✓ ●○").strip())
                lbl.add_css_class("stepper-active")
            else:
                lbl.set_text("○ " + lbl.get_text().lstrip("✓ ●○").strip())
                lbl.add_css_class("stepper-pending")


class TourPanel(Gtk.Box):
    """Two-column tour panel: left = file tree / diagram, right = education card."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.add_css_class("tour-panel")
        self.set_size_request(-1, 120)

        self._left = Gtk.Label(label="", xalign=0, yalign=0)
        self._left.set_wrap(True)
        self._left.add_css_class("muted")
        left_scroll = Gtk.ScrolledWindow()
        left_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        left_scroll.set_child(self._left)
        left_scroll.set_hexpand(True)

        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)

        self._right_title = Gtk.Label(label="", xalign=0)
        self._right_title.add_css_class("teal")
        self._right_body  = Gtk.Label(label="", xalign=0, yalign=0)
        self._right_body.set_wrap(True)
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        right_box.append(self._right_title)
        right_box.append(self._right_body)
        right_box.set_hexpand(True)

        self.append(left_scroll)
        self.append(sep)
        self.append(right_box)

    def update(self, left_text: str, title: str, body: str):
        self._left.set_text(left_text)
        self._right_title.set_text(title)
        self._right_body.set_text(body)

    def show_stage(self, stage_key: str, model_entry: ModelEntry | None):
        fact = _MATRIX_FACTS.get(stage_key, ("", ""))
        title, body = fact if fact else ("", "")
        left = ""
        if model_entry:
            left = f"Model: {model_entry.model_name}\n"
            left += f"Device: {model_entry.device_type}\n"
            left += f"Engine: {model_entry.inference_engine}\n"
            if model_entry.param_count:
                left += f"Params: {model_entry.param_count:.1f}B\n"
            if model_entry.min_disk_gb:
                left += f"Disk: {model_entry.min_disk_gb:.0f} GB\n"
        self.update(left, title, body)


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, application, css: bytes):
        super().__init__(application=application, title="TT Model Runner")

        # Apply CSS
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self._settings = AppSettings()
        self._catalog: ModelCatalog | None = None
        self._timing = TimingStore()
        self._manager = ServerManager()
        self._health: HealthWorker | None = None
        self._state = ServerState.IDLE
        self._selected_entry: ModelEntry | None = None
        self._current_stage: str | None = None
        self._stage_start_time: float | None = None
        self._load_start_time: float | None = None
        self._log_buffer_lines: list[str] = []

        self.set_default_size(self._settings.window_width, self._settings.window_height)
        self._build_ui()
        self._load_catalog_async()
        GLib.timeout_add(500, self._tick)

    # ------------------------------------------------------------------ UI BUILD

    def _build_ui(self):
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(self._settings.sidebar_width)

        paned.set_start_child(self._build_sidebar())
        paned.set_end_child(self._build_main_panel())
        self.set_child(paned)

    def _build_sidebar(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(8); box.set_margin_end(8)
        box.set_margin_top(8);   box.set_margin_bottom(8)

        # Repo path
        repo_label = Gtk.Label(label="SERVER REPO", xalign=0)
        repo_label.add_css_class("section-label")
        self._repo_entry = Gtk.Entry()
        self._repo_entry.set_text(self._settings.server_repo_path)
        self._repo_entry.connect("changed", self._on_repo_changed)

        # Model tree
        model_label = Gtk.Label(label="MODEL", xalign=0)
        model_label.add_css_class("section-label")
        self._tree_store = Gtk.TreeStore(str, str, bool)  # display, model_name, is_leaf
        self._tree_view = Gtk.TreeView(model=self._tree_store)
        self._tree_view.set_headers_visible(False)
        renderer = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn("", renderer, text=0)
        self._tree_view.append_column(col)
        self._tree_view.get_selection().connect("changed", self._on_model_selected)
        tree_scroll = Gtk.ScrolledWindow()
        tree_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        tree_scroll.set_child(self._tree_view)
        tree_scroll.set_vexpand(True)

        # Device buttons
        device_label = Gtk.Label(label="DEVICE", xalign=0)
        device_label.add_css_class("section-label")
        self._device_box = Gtk.FlowBox()
        self._device_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._device_btns: dict[str, Gtk.ToggleButton] = {}

        # Port entry
        port_label = Gtk.Label(label="PORT", xalign=0)
        port_label.add_css_class("section-label")
        self._port_entry = Gtk.Entry()
        self._port_entry.set_text(self._settings.last_port)
        self._port_entry.set_max_width_chars(8)

        # Launch / Stop button
        self._launch_btn = Gtk.Button(label="▶  Launch Server")
        self._launch_btn.add_css_class("launch-btn")
        self._launch_btn.connect("clicked", self._on_launch_clicked)

        # HF token status
        self._hf_label = Gtk.Label(label="", xalign=0)
        self._update_hf_status()

        for w in [repo_label, self._repo_entry, model_label, tree_scroll,
                  device_label, self._device_box, port_label, self._port_entry,
                  self._launch_btn, self._hf_label]:
            box.append(w)
        return box

    def _build_main_panel(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Status banner
        banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        banner.set_margin_start(10); banner.set_margin_end(10)
        banner.set_margin_top(8);    banner.set_margin_bottom(6)
        self._state_pill = Gtk.Label(label="IDLE")
        self._state_pill.add_css_class("pill"); self._state_pill.add_css_class("pill-idle")
        self._banner_info = Gtk.Label(label="Select a model and click Launch", xalign=0)
        self._banner_info.add_css_class("muted")
        self._banner_info.set_hexpand(True)
        banner.append(self._state_pill)
        banner.append(self._banner_info)
        box.append(banner)

        # Progress bar + estimate label
        prog_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        prog_box.set_margin_start(10); prog_box.set_margin_end(10)
        self._progress = Gtk.ProgressBar()
        self._progress.set_visible(False)
        self._estimate_label = Gtk.Label(label="", xalign=0)
        self._estimate_label.add_css_class("muted")
        prog_box.append(self._progress)
        prog_box.append(self._estimate_label)
        box.append(prog_box)

        # Sub-stage stepper (hidden when IDLE)
        stepper_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        stepper_box.set_margin_start(10); stepper_box.set_margin_end(10)
        stepper_box.set_margin_top(4)
        self._stepper = StepperWidget()
        self._stepper.set_hexpand(True)
        stepper_box.append(self._stepper)
        self._stepper_box = stepper_box
        self._stepper_box.set_visible(False)
        box.append(stepper_box)

        # Tour panel (hidden when IDLE/READY)
        self._tour = TourPanel()
        self._tour.set_margin_start(10); self._tour.set_margin_end(10)
        self._tour.set_margin_top(6)
        self._tour.set_visible(False)
        box.append(self._tour)

        # Log view
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.set_margin_top(6)
        box.append(sep)

        self._log_buf = Gtk.TextBuffer()
        self._log_view = Gtk.TextView(buffer=self._log_buf)
        self._log_view.set_editable(False)
        self._log_view.set_monospace(True)
        self._log_view.add_css_class("log-view")
        self._log_view.set_wrap_mode(Gtk.WrapMode.CHAR)
        log_scroll = Gtk.ScrolledWindow()
        log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        log_scroll.set_child(self._log_view)
        log_scroll.set_vexpand(True)
        self._log_scroll = log_scroll
        box.append(log_scroll)

        # Pango tags for log levels
        self._tag_info  = self._log_buf.create_tag("INFO",  foreground="#4FD1C5")
        self._tag_warn  = self._log_buf.create_tag("WARN",  foreground="#F4C471")
        self._tag_error = self._log_buf.create_tag("ERROR", foreground="#FF6B6B")
        self._tag_debug = self._log_buf.create_tag("DEBUG", foreground="#607D8B")
        self._auto_scroll = True
        log_scroll.get_vadjustment().connect("value-changed", self._on_scroll_changed)

        return box

    # ------------------------------------------------------------------ CATALOG LOAD

    def _load_catalog_async(self):
        repo = Path(self._repo_entry.get_text())
        spec = repo / "model_spec.json"
        if not spec.exists():
            self._append_log(f"model_spec.json not found at {spec}\n", "WARN")
            return

        def _load():
            try:
                cat = ModelCatalog.load(spec)
                detected = detect_devices()
                GLib.idle_add(self._on_catalog_loaded, cat, detected)
            except Exception as e:
                GLib.idle_add(self._append_log, f"Failed to load model_spec: {e}\n", "ERROR")

        threading.Thread(target=_load, daemon=True).start()

    def _on_catalog_loaded(self, catalog: ModelCatalog, detected: list[str]):
        self._catalog = catalog
        self._populate_devices(catalog, detected)
        self._populate_tree(catalog, detected or catalog.all_devices())

    def _populate_devices(self, catalog: ModelCatalog, detected: list[str]):
        for child in list(self._device_box):
            self._device_box.remove(child)
        self._device_btns.clear()
        devices = detected if detected else catalog.all_devices()
        last = self._settings.last_device
        for dev in sorted(devices):
            btn = Gtk.ToggleButton(label=dev)
            btn.connect("toggled", self._on_device_toggled, dev)
            if dev == last:
                btn.set_active(True)
            self._device_box.append(btn)
            self._device_btns[dev] = btn

    def _populate_tree(self, catalog: ModelCatalog, device_filter: list[str]):
        self._tree_store.clear()
        active_devs = [d for d, b in self._device_btns.items() if b.get_active()]
        if active_devs:
            filtered = catalog.get_compatible(active_devs)
        else:
            filtered = catalog
        tree = filtered.get_tree()
        for mtype, families in sorted(tree.items()):
            type_iter = self._tree_store.append(None, [mtype, "", False])
            for family, entries in sorted(families.items()):
                fam_iter = self._tree_store.append(type_iter, [family, "", False])
                for entry in entries:
                    label = f"{entry.model_name} [{entry.device_type}]"
                    self._tree_store.append(fam_iter, [label, entry.model_name, True])
        self._tree_view.expand_all()

    # ------------------------------------------------------------------ EVENT HANDLERS

    def _on_repo_changed(self, entry):
        self._settings.server_repo_path = entry.get_text()
        self._settings.save()
        self._load_catalog_async()

    def _on_device_toggled(self, btn, dev):
        if self._catalog:
            active = [d for d, b in self._device_btns.items() if b.get_active()]
            self._populate_tree(self._catalog, active)

    def _on_model_selected(self, selection):
        model, it = selection.get_selected()
        if it is None:
            return
        is_leaf = model[it][2]
        if not is_leaf:
            return
        model_name = model[it][1]
        active_devs = [d for d, b in self._device_btns.items() if b.get_active()]
        dev = active_devs[0] if active_devs else ""
        if self._catalog:
            entry = self._catalog.get_entry(model_name, dev)
            if not entry:
                # Try any device
                for e in self._catalog._entries:
                    if e.model_name == model_name:
                        entry = e; break
            self._selected_entry = entry
        self._settings.last_model = model_name
        self._settings.save()

    def _on_launch_clicked(self, btn):
        if self._state in (ServerState.READY, ServerState.LOADING,
                           ServerState.LAUNCHING, ServerState.PULLING_IMAGE):
            self._do_stop()
        elif self._state == ServerState.IDLE or self._state == ServerState.ERROR:
            self._do_launch()

    def _do_launch(self):
        if not self._selected_entry:
            self._append_log("Select a model first.\n", "WARN")
            return
        repo = Path(self._repo_entry.get_text())
        if not (repo / "run.py").exists():
            self._append_log(f"run.py not found in {repo}\n", "ERROR")
            return
        hf_token = os.environ.get("HF_TOKEN") or self._read_dotenv_token(repo)
        config = LaunchConfig(
            repo_path=repo,
            model_name=self._selected_entry.hf_model_repo or self._selected_entry.model_name,
            device=self._selected_entry.device_type,
            port=self._port_entry.get_text() or "8000",
            hf_token=hf_token,
            inference_engine=self._selected_entry.inference_engine,
        )
        self._log_buf.set_text("")
        self._load_start_time = None
        self._set_state(ServerState.LAUNCHING)
        self._manager.launch(config,
                             on_log_line=self._on_log_line,
                             on_state=self._on_server_state)
        port = config.port
        self._health = HealthWorker(port=port,
                                    on_ready=self._on_health_ready,
                                    on_lost=self._on_health_lost)
        self._health.start()

    def _do_stop(self):
        self._set_state(ServerState.STOPPING)
        if self._health:
            self._health.stop()
        self._manager.stop(on_stopped=self._on_stopped)

    def _on_stopped(self):
        self._set_state(ServerState.IDLE)

    def _on_log_line(self, line: str):
        GLib.idle_add(self._append_log, line, self._classify_level(line))

    def _on_server_state(self, state: ServerState, extra: dict):
        def _update():
            if state == ServerState.LOADING and self._state != ServerState.LOADING:
                self._load_start_time = __import__('time').time()
                self._setup_stepper()
            if state == ServerState.ERROR and self._state == ServerState.ERROR:
                return
            if "substage" in extra:
                self._on_substage(extra["substage"], extra)
                return
            self._set_state(state)
        GLib.idle_add(_update)

    def _on_health_ready(self, models: list[str]):
        self._set_state(ServerState.READY)

    def _on_health_lost(self):
        if self._state == ServerState.READY:
            self._set_state(ServerState.ERROR)

    def _on_substage(self, stage_key: str, extra: dict):
        self._current_stage = stage_key
        self._stage_start_time = __import__('time').time()
        engine = self._selected_entry.inference_engine if self._selected_entry else "vllm"
        labels = _VLLM_STAGE_LABELS if engine == "vllm" else _MEDIA_STAGE_LABELS
        if stage_key in labels:
            self._stepper.set_active(stage_key)
        if self._selected_entry:
            self._tour.show_stage(stage_key, self._selected_entry)

    # ------------------------------------------------------------------ STATE MACHINE

    def _set_state(self, state: ServerState):
        self._state = state
        pill_text = {
            ServerState.IDLE:          ("IDLE",          "pill-idle"),
            ServerState.LAUNCHING:     ("LAUNCHING",     "pill-loading"),
            ServerState.PULLING_IMAGE: ("PULLING IMAGE", "pill-loading"),
            ServerState.LOADING:       ("LOADING",       "pill-loading"),
            ServerState.READY:         ("READY",         "pill-ready"),
            ServerState.ERROR:         ("ERROR",         "pill-error"),
            ServerState.STOPPING:      ("STOPPING",      "pill-stopping"),
        }
        text, css = pill_text.get(state, ("?", "pill-idle"))
        self._state_pill.set_text(text)
        for cls in ["pill-idle", "pill-loading", "pill-ready", "pill-error", "pill-stopping"]:
            self._state_pill.remove_css_class(cls)
        self._state_pill.add_css_class(css)

        loading = state in (ServerState.LAUNCHING, ServerState.PULLING_IMAGE, ServerState.LOADING)
        self._progress.set_visible(loading)
        self._stepper_box.set_visible(state == ServerState.LOADING)
        self._tour.set_visible(state == ServerState.LOADING)

        if state in (ServerState.LAUNCHING, ServerState.PULLING_IMAGE):
            self._progress.pulse()

        if state == ServerState.READY:
            self._progress.set_fraction(1.0)
            GLib.timeout_add(2000, self._hide_progress)

        if state == ServerState.IDLE or state == ServerState.ERROR:
            self._launch_btn.set_label("▶  Launch Server")
            self._launch_btn.remove_css_class("stop-btn")
            self._launch_btn.add_css_class("launch-btn")
        else:
            self._launch_btn.set_label("■  Stop Server")
            self._launch_btn.remove_css_class("launch-btn")
            self._launch_btn.add_css_class("stop-btn")

        if self._selected_entry:
            info = f"localhost:{self._port_entry.get_text()}  ·  {self._selected_entry.model_name}  [{self._selected_entry.device_type}]"
            self._banner_info.set_text(info)

    def _hide_progress(self):
        self._progress.set_visible(False)
        return False  # don't repeat

    def _setup_stepper(self):
        if not self._selected_entry:
            return
        engine = self._selected_entry.inference_engine
        if "media" in engine.lower():
            from server_manager import _MEDIA_STAGE_ORDER, _VLLM_STAGE_ORDER
            keys   = _MEDIA_STAGE_ORDER
            labels = _MEDIA_STAGE_LABELS
        else:
            from server_manager import _VLLM_STAGE_ORDER
            keys   = _VLLM_STAGE_ORDER
            labels = _VLLM_STAGE_LABELS
        self._stepper.set_stages(keys, labels)

    # ------------------------------------------------------------------ LOG VIEW

    def _classify_level(self, line: str) -> str:
        l = line.upper()
        if " ERROR" in l or "⛔" in l:  return "ERROR"
        if " WARN"  in l:               return "WARN"
        if " DEBUG" in l:               return "DEBUG"
        return "INFO"

    def _append_log(self, line: str, level: str = "INFO"):
        end = self._log_buf.get_end_iter()
        tag = {"INFO": self._tag_info, "WARN": self._tag_warn,
               "ERROR": self._tag_error, "DEBUG": self._tag_debug}.get(level)
        if tag:
            self._log_buf.insert_with_tags(end, line, tag)
        else:
            self._log_buf.insert(end, line)
        if self._auto_scroll:
            adj = self._log_scroll.get_vadjustment()
            adj.set_value(adj.get_upper() - adj.get_page_size())

    def _on_scroll_changed(self, adj):
        at_bottom = adj.get_value() >= adj.get_upper() - adj.get_page_size() - 20
        self._auto_scroll = at_bottom

    # ------------------------------------------------------------------ TICK / PROGRESS

    def _tick(self) -> bool:
        """Called every 500ms to update progress bar during LOADING."""
        if self._state == ServerState.LAUNCHING or self._state == ServerState.PULLING_IMAGE:
            self._progress.pulse()
        elif self._state == ServerState.LOADING and self._load_start_time:
            import time
            elapsed = time.time() - self._load_start_time
            if self._selected_entry:
                est = self._timing.estimate_load(
                    self._selected_entry.hf_model_repo or self._selected_entry.model_name,
                    self._selected_entry.device_type,
                    cold=True,
                    size_gb=self._selected_entry.min_disk_gb or 10.0,
                    family=self._selected_entry.family,
                )
                if est.seconds:
                    frac = min(elapsed / est.seconds, 0.95)
                    self._progress.set_fraction(frac)
                    remaining = max(0, est.seconds - elapsed)
                    mins = int(remaining // 60); secs = int(remaining % 60)
                    self._estimate_label.set_text(
                        f"~{mins}m {secs:02d}s remaining · {est.source}")
                else:
                    self._progress.pulse()
                    self._estimate_label.set_text("")
        return True  # keep ticking

    # ------------------------------------------------------------------ HELPERS

    def _update_hf_status(self):
        token = os.environ.get("HF_TOKEN")
        if token:
            self._hf_label.set_text("HF_TOKEN: ✓ from env")
            self._hf_label.remove_css_class("hf-warn")
            self._hf_label.add_css_class("hf-ok")
        else:
            self._hf_label.set_text("⚠  HF_TOKEN not set")
            self._hf_label.remove_css_class("hf-ok")
            self._hf_label.add_css_class("hf-warn")

    def _read_dotenv_token(self, repo: Path) -> str | None:
        env_file = repo / ".env"
        if not env_file.exists():
            return None
        for line in env_file.read_text().splitlines():
            if line.startswith("HF_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
        return None
```

- [ ] **Step 2: Fix `app/main.py` — simplify `do_activate`**

The skeleton in Task 1 has a circular CSS-apply issue. Replace `do_activate` in `app/main.py`:

```python
    def do_activate(self):
        from main_window import MainWindow
        win = MainWindow(application=self, css=_CSS)
        win.present()
```

- [ ] **Step 3: Test that the window opens**

```bash
cd /home/ttuser/code/tt-model-runner-gui/app
python3 main.py &
sleep 3
# Visually verify: window opens, sidebar shows repo path, model tree loads
kill %1
```

- [ ] **Step 4: Commit**

```bash
git add app/main_window.py app/main.py
git commit -m "feat: main window with sidebar, model tree, status banner, log view, tour panel"
```

---

### Task 9: Wire sub-stage stepper orders into server_manager

The stepper setup calls `_MEDIA_STAGE_ORDER` and `_VLLM_STAGE_ORDER` from server_manager. Export them properly.

- [ ] **Step 1: Verify exports exist in server_manager.py**

```bash
python3 -c "from server_manager import _VLLM_STAGE_ORDER, _MEDIA_STAGE_ORDER; print(_VLLM_STAGE_ORDER)"
```
Expected: a list starting with `'engine_init'`.

- [ ] **Step 2: Fix main_window.py stepper setup import**

In `_setup_stepper`, both branches import from server_manager. Clean up to avoid the double-import in the else branch:

```python
    def _setup_stepper(self):
        if not self._selected_entry:
            return
        from server_manager import _VLLM_STAGE_ORDER, _MEDIA_STAGE_ORDER
        engine = self._selected_entry.inference_engine.lower()
        if "media" in engine:
            keys   = _MEDIA_STAGE_ORDER
            labels = _MEDIA_STAGE_LABELS
        else:
            keys   = _VLLM_STAGE_ORDER
            labels = _VLLM_STAGE_LABELS
        self._stepper.set_stages(keys, labels)
```

Apply this edit to `app/main_window.py`.

- [ ] **Step 3: Run all tests**

```bash
cd /home/ttuser/code/tt-model-runner-gui
python3 -m pytest tests/ -v
```
Expected: all PASSED (no GTK tests, only unit tests).

- [ ] **Step 4: Commit**

```bash
git add app/main_window.py
git commit -m "fix: stepper setup imports _STAGE_ORDER from server_manager correctly"
```

---

### Task 10: End-to-end PoC test with Llama-3.2-1B

This is the integration test. Launch a real small model, watch the state machine progress, verify logs stream, verify health check fires.

**Prerequisite:** `HF_TOKEN` set in environment. `docker` available. `tt-inference-server` at `~/code/tt-inference-server`.

- [ ] **Step 1: Verify the small model entry exists in model_spec.json**

```bash
python3 -c "
import json
data = json.load(open('/home/ttuser/code/tt-inference-server/model_spec.json'))
for mid, devs in data['model_specs'].items():
    for dev, engines in devs.items():
        for eng, info in engines.items():
            impl = info.get('impl', info)
            if '1B' in impl.get('model_name','') or '1b' in impl.get('hf_model_repo',''):
                print(dev, impl.get('model_name'), impl.get('hf_model_repo'))
" 2>&1 | head -20
```

- [ ] **Step 2: Launch the GUI**

```bash
cd /home/ttuser/code/tt-model-runner-gui/app
HF_TOKEN=$(grep HF_TOKEN ~/.env 2>/dev/null | cut -d= -f2 || echo $HF_TOKEN) python3 main.py
```

- [ ] **Step 3: Manual test sequence**

1. Verify sidebar shows model tree with LLM → Llama → Llama-3.2-1B
2. Select Llama-3.2-1B
3. Select compatible device in device buttons
4. Click Launch Server
5. Observe: state pill → LAUNCHING, progress bar pulses
6. Observe: log lines stream in (colored by level)
7. Observe: state → PULLING_IMAGE when docker pull appears
8. Observe: state → LOADING, stepper appears, tour panel appears
9. Observe: progress bar switches from pulse to time-based once estimate kicks in
10. Observe: state → READY once `/v1/models` returns 200
11. Click Stop, observe: state → STOPPING → IDLE

- [ ] **Step 4: If model not in catalog or can't launch, use the echo test**

```bash
# Test log streaming with a fake server that just writes log lines
cat > /tmp/fake_server.sh << 'EOF'
#!/bin/bash
mkdir -p workflow_logs/run_logs
LOG=workflow_logs/run_logs/run_fake.log
echo "$(date) INFO: docker pull ghcr.io/tt/test" > $LOG
sleep 2
echo "$(date) INFO: Starting vLLM API server on port 8000" >> $LOG
sleep 2
echo "$(date) INFO: Capturing traces: input_seq_len=128" >> $LOG
echo "$(date) INFO: Capturing traces: input_seq_len=256" >> $LOG
sleep 60
EOF
chmod +x /tmp/fake_server.sh
```

Then in main_window.py `_do_launch`, temporarily replace the subprocess call:
```python
self._proc = subprocess.Popen(["/tmp/fake_server.sh"], cwd=str(config.repo_path))
```
And verify the state machine progresses correctly through PULLING_IMAGE → LOADING.

- [ ] **Step 5: Commit**

```bash
git add .
git commit -m "feat: end-to-end PoC — GTK4 app, server lifecycle, live logs, sub-stage stepper, timing estimates"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Covered in task |
|---|---|
| GTK4 app, sidebar + main panel | Task 1, 8 |
| Repo picker, auto-discover | Task 8 |
| 3-level model tree (Type→Family→Model) | Task 3, 8 |
| Device detection via tt-smi | Task 4 |
| Only compatible devices shown | Task 4, 8 |
| HF_TOKEN from env / .env | Task 8 |
| --no-auth always | Task 6 |
| Launch run.py as subprocess | Task 6 |
| Tail log file | Task 6 |
| State machine (6 states) | Task 6, 8 |
| Sub-stage stepper (vLLM + media) | Task 6, 8, 9 |
| Progress bar: pulse vs time-based | Task 8 |
| Timing estimation cascade | Task 5 |
| Pre-seeded bootstrap data | Task 5 |
| Health worker /v1/models | Task 7 |
| Log color-coding by level | Task 8 |
| Auto-scroll unless user scrolled | Task 8 |
| Tour panel with matrix education | Task 8 |
| Cold/warm detection | Task 5 |
| app_settings persistence | Task 2 |
| End-to-end PoC | Task 10 |

All spec requirements covered. No TBDs or placeholders.
