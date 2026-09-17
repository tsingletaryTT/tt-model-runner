# tt-model-manager Community Models Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add tt-model-manager community bundles as a second, first-class model source (discovered via `tt model list --community`, launched via `tt-model serve`), shown in their own "Community" sidebar section in both GTK and TUI, without touching the existing tt-inference-server `run.py`/Docker launch path.

**Architecture:** `ModelEntry` gains a `source` field (`"inference_server"` | `"community"`). A new `community_catalog.py` shells the `tt` CLI and parses results into `ModelEntry` objects merged into the existing `ModelCatalog`. A new `TtModelLauncher` (parallel to `ServerManager`/`DevImageLauncher`) shells `tt-model serve`/`tt-model stop`, reusing the existing `LogParser` and `HealthWorker` for state/readiness detection. `AppController` gains `launch_community()` alongside `launch()`/`launch_dev_image()`; both GTK and TUI views branch to it based on `entry.source`.

**Tech Stack:** Python 3, GTK4 (PyGObject), Textual, pytest. No new third-party dependencies — shells the `tt`/`tt-model` CLI binaries via `subprocess`.

**Spec:** [docs/superpowers/specs/2026-09-17-tt-cli-community-models-design.md](../specs/2026-09-17-tt-cli-community-models-design.md)

## Global Constraints

- Do not modify `run.py` invocation, GHCR resolution, docker shim, or auto-remediation logic in `app/server_manager.py` — confirmed still correct against current tt-inference-server images.
- No new dependency on `tt-cli`'s own `tt serve` auto-dispatch — the app already knows an entry's source, so `TtModelLauncher` calls `tt-model serve` directly.
- Community bundle launches get no auto-remediation/retry logic in this plan — errors surface as `ServerState.ERROR` with the raw `tt-model serve` output.
- Every new `on_*` controller callback must be added to `ViewContract` (`tests/test_controller_contract.py`) and both stubs before use elsewhere, per the project's "Adding a new feature to both UIs" workflow.
- Missing `tt`/`tt-model` binaries must degrade gracefully (empty Community section + a hint), never raise or block the rest of the app.

---

### Task 1: `ModelEntry.source` field + catalog merge support

**Files:**
- Modify: `app/model_catalog.py`
- Test: `tests/test_model_catalog.py`

**Interfaces:**
- Produces: `ModelEntry.source: str` (default `"inference_server"`); `ModelCatalog.merge_community(entries: List[ModelEntry]) -> None`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_model_catalog.py` (uses `ModelCatalog([])` directly —
its constructor already accepts a plain list per `app/model_catalog.py:52-53`,
so no spec file needs parsing for this test):

```python
def test_model_entry_defaults_to_inference_server_source():
    from model_catalog import ModelEntry
    entry = ModelEntry(
        model_id="x", model_name="x", display_name="x", hf_model_repo="x",
        model_type="LLM", family="x", device_type="N150",
        inference_engine="vllm", docker_image="", status="COMPLETE",
        param_count=None, min_disk_gb=None, min_ram_gb=None,
    )
    assert entry.source == "inference_server"


def test_merge_community_appends_entries():
    from model_catalog import ModelEntry
    cat = ModelCatalog([])
    community_entry = ModelEntry(
        model_id="org/bundle", model_name="org/bundle", display_name="bundle",
        hf_model_repo="org/weights", model_type="COMMUNITY", family="org",
        device_type="UNKNOWN", inference_engine="vllm", docker_image="",
        status="COMMUNITY", param_count=None, min_disk_gb=None, min_ram_gb=None,
        source="community",
    )
    cat.merge_community([community_entry])
    all_entries = cat.all_entries()
    assert len(all_entries) == 1
    assert all_entries[0].source == "community"
    assert all_entries[0].model_id == "org/bundle"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ttuser/code/tt-model-runner && PYTHONPATH=app pytest tests/test_model_catalog.py -k "source or merge_community" -v`
Expected: FAIL — `ModelEntry.__init__() got an unexpected keyword argument 'source'` and `AttributeError: 'ModelCatalog' object has no attribute 'merge_community'`

- [ ] **Step 3: Implement**

In `app/model_catalog.py`, add `source` as the last field of the `ModelEntry`
dataclass (after `min_ram_gb`, so it has a default and doesn't break existing
positional construction):

```python
    min_ram_gb: Optional[float]
    source: str = "inference_server"   # "inference_server" | "community"
```

Add a method to `ModelCatalog` (near `all_entries`):

```python
    def merge_community(self, entries: List[ModelEntry]) -> None:
        """Append community-sourced entries (from community_catalog.py).

        Idempotent by model_id: re-merging (e.g. after a catalog refresh)
        replaces existing community entries with the same model_id instead
        of duplicating them.
        """
        existing_ids = {e.model_id for e in entries}
        self._entries = [
            e for e in self._entries
            if not (e.source == "community" and e.model_id in existing_ids)
        ] + list(entries)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ttuser/code/tt-model-runner && PYTHONPATH=app pytest tests/test_model_catalog.py -v`
Expected: PASS (all tests in the file, including the two new ones and the
pre-existing ones — confirms the new default field didn't break any
positional-argument construction elsewhere in the file's own tests)

- [ ] **Step 5: Commit**

```bash
git add app/model_catalog.py tests/test_model_catalog.py
git commit -m "feat: add ModelEntry.source and ModelCatalog.merge_community"
```

---

### Task 2: `community_catalog.py` — discover bundles via `tt model list --community`

**Files:**
- Create: `app/community_catalog.py`
- Test: `tests/test_community_catalog.py`

**Interfaces:**
- Consumes: `model_catalog.ModelEntry` (Task 1's `source` field)
- Produces: `parse_community_list(raw: str) -> List[ModelEntry]`, `fetch_community_entries() -> List[ModelEntry]`, `load_async(on_done: Callable[[List[ModelEntry]], None]) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_community_catalog.py`:

```python
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
from community_catalog import parse_community_list, fetch_community_entries

JSON_OUTPUT = json.dumps([
    {"id": "episod/tt-animatediff", "kind": "tt-dit-server",
     "arch": "blackhole", "mesh": "p150", "weights": "CompVis/stable-diffusion-v1-4"},
    {"id": "acme/llama-fast", "kind": "vllm",
     "arch": "wormhole", "mesh": "t3000", "weights": "meta-llama/Llama-3.2-1B"},
])

TEXT_OUTPUT = """\
NAME                     KIND           ARCH        MESH
episod/tt-animatediff    tt-dit-server  blackhole   p150
acme/llama-fast          vllm           wormhole    t3000
"""


def test_parse_community_list_json():
    entries = parse_community_list(JSON_OUTPUT)
    assert len(entries) == 2
    assert entries[0].model_id == "episod/tt-animatediff"
    assert entries[0].source == "community"
    assert entries[0].inference_engine == "tt-dit-server"
    assert entries[0].device_type == "P150"
    assert entries[0].hf_model_repo == "CompVis/stable-diffusion-v1-4"
    assert entries[1].device_type == "T3K"


def test_parse_community_list_text_fallback():
    entries = parse_community_list(TEXT_OUTPUT)
    assert len(entries) == 2
    assert entries[0].model_id == "episod/tt-animatediff"
    assert entries[0].inference_engine == "tt-dit-server"
    assert entries[1].model_id == "acme/llama-fast"


def test_parse_community_list_unmapped_device_is_unknown():
    raw = json.dumps([{"id": "x/y", "kind": "vllm", "arch": "exotic", "mesh": ""}])
    entries = parse_community_list(raw)
    assert entries[0].device_type == "UNKNOWN"


def test_parse_community_list_empty_input():
    assert parse_community_list("") == []
    assert parse_community_list("   \n  ") == []


def test_fetch_community_entries_returns_empty_when_tt_missing():
    with patch("community_catalog.shutil.which", return_value=None):
        assert fetch_community_entries() == []


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/ttuser/code/tt-model-runner && PYTHONPATH=app pytest tests/test_community_catalog.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'community_catalog'`

- [ ] **Step 3: Implement**

Create `app/community_catalog.py`:

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Discover tt-model-manager community bundles via `tt model list --community`.

Community bundles are self-contained (no docker_image, no model_spec-style
device_type) — this module maps what's available onto ModelEntry so the rest
of the app (filtering, tree building, launching) doesn't need to know the
difference beyond checking `source`.
"""
import json
import shutil
import subprocess
import threading
from typing import Callable, List, Optional

from model_catalog import ModelEntry

_TIMEOUT = 15

# Maps tt-model-manager arch/mesh labels (lowercase) to model_spec.json
# device_type strings used elsewhere in the app (device_detector.py,
# compat_catalog.py). Unrecognized values fall back to "UNKNOWN".
_DEVICE_MAP = {
    "p100": "P100", "p150": "P150", "p300": "P300", "p300x2": "P300X2",
    "n150": "N150", "n300": "N300",
    "t3000": "T3K", "t3k": "T3K",
    "galaxy": "GALAXY",
}


def _map_device_type(mesh: str, arch: str) -> str:
    key = (mesh or arch or "").lower()
    return _DEVICE_MAP.get(key, "UNKNOWN")


def _entry_from_record(rec: dict) -> ModelEntry:
    bundle_id = rec.get("id") or rec.get("name") or rec.get("bundle_id") or ""
    display = bundle_id.split("/")[-1] if "/" in bundle_id else bundle_id
    org = bundle_id.split("/")[0] if "/" in bundle_id else "community"
    mesh = rec.get("mesh") or rec.get("mesh_device") or ""
    arch = rec.get("arch") or ""
    return ModelEntry(
        model_id=bundle_id,
        model_name=bundle_id,
        display_name=display,
        hf_model_repo=rec.get("weights") or rec.get("weights_repo") or bundle_id,
        model_type="COMMUNITY",
        family=org,
        device_type=_map_device_type(mesh, arch),
        inference_engine=rec.get("kind") or "vllm",
        docker_image="",
        status="COMMUNITY",
        param_count=None,
        min_disk_gb=None,
        min_ram_gb=None,
        source="community",
    )


def parse_community_list(raw: str) -> List[ModelEntry]:
    """Parse `tt model list --community` output into ModelEntry objects.

    Tries JSON first (a list of bundle dicts, or {"models": [...]});  falls
    back to a whitespace-separated text table (NAME KIND ARCH MESH, one
    bundle per line) since the CLI's machine-readable support was
    unconfirmed at design time — both must be handled.
    """
    raw = raw.strip()
    if not raw:
        return []

    try:
        data = json.loads(raw)
        records = data if isinstance(data, list) else data.get("models", [])
        return [_entry_from_record(rec) for rec in records]
    except (json.JSONDecodeError, AttributeError):
        pass

    entries: List[ModelEntry] = []
    for line in raw.splitlines():
        line = line.strip()
        parts = line.split()
        if not parts or "/" not in parts[0]:
            continue
        rec = {
            "id": parts[0],
            "kind": parts[1] if len(parts) > 1 else "vllm",
            "arch": parts[2] if len(parts) > 2 else "",
            "mesh": parts[3] if len(parts) > 3 else "",
        }
        entries.append(_entry_from_record(rec))
    return entries


def _run_tt(args: List[str]) -> Optional[str]:
    tt_bin = shutil.which("tt")
    if not tt_bin:
        return None
    try:
        result = subprocess.run(
            [tt_bin] + args, capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def fetch_community_entries() -> List[ModelEntry]:
    """Shell `tt model list --community` and parse the result.

    Tries --json first; if that flag isn't supported (or any other failure),
    retries without it and parses the text-table fallback. Returns [] if the
    `tt` CLI isn't installed or both attempts fail — an empty community list
    is "nothing to show", not an error the caller needs to surface.
    """
    if not shutil.which("tt"):
        return []
    out = _run_tt(["model", "list", "--community", "--json"])
    if out is None:
        out = _run_tt(["model", "list", "--community"])
    if out is None:
        return []
    return parse_community_list(out)


def load_async(on_done: Callable[[List[ModelEntry]], None]) -> None:
    """Fetch community bundles in a background thread.

    on_done(entries) — entries is [] on any failure (missing CLI, timeout,
    parse failure). Called from a background thread — caller dispatches to
    the UI event loop if needed (mirrors compat_catalog.load_async).
    """
    def _run():
        try:
            on_done(fetch_community_entries())
        except Exception:
            on_done([])

    threading.Thread(target=_run, daemon=True).start()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/ttuser/code/tt-model-runner && PYTHONPATH=app pytest tests/test_community_catalog.py -v`
Expected: PASS (all 6 tests)

- [ ] **Step 5: Commit**

```bash
git add app/community_catalog.py tests/test_community_catalog.py
git commit -m "feat: add community_catalog.py to discover tt-model-manager bundles"
```

---

### Task 3: `TtModelLauncher` — launch community bundles via `tt-model serve`

**Files:**
- Create: `app/tt_model_launcher.py`
- Test: `tests/test_tt_model_launcher.py`

**Interfaces:**
- Consumes: `server_manager.LogParser`, `server_manager.ServerState`
- Produces: `TtModelLaunchConfig(bundle_id, port="8000", profile="")`, `TtModelLauncher.launch(config, on_log_line, on_state)`, `TtModelLauncher.stop()`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tt_model_launcher.py`:

```python
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
from tt_model_launcher import TtModelLauncher, TtModelLaunchConfig
from server_manager import ServerState


def _fake_popen(lines, returncode=0):
    proc = MagicMock()
    proc.stdout = iter(lines)
    proc.wait.return_value = returncode
    return proc


def test_launch_builds_expected_command_and_reports_launching():
    launcher = TtModelLauncher()
    log_lines, states = [], []
    with patch("tt_model_launcher.shutil.which", return_value="/usr/bin/tt-model"), \
         patch("tt_model_launcher.subprocess.Popen") as mock_popen:
        mock_popen.return_value = _fake_popen(["Starting vLLM API server\n"])
        launcher.launch(
            TtModelLaunchConfig(bundle_id="acme/llama-fast", port="8001"),
            log_lines.append, states.append,
        )
        launcher._thread.join(timeout=5)

    args = mock_popen.call_args[0][0]
    assert args == ["/usr/bin/tt-model", "serve", "acme/llama-fast", "--port", "8001"]
    assert ServerState.LAUNCHING in states
    assert ServerState.LOADING in states  # "Starting vLLM API server" → LOADING via LogParser


def test_launch_includes_profile_flag_when_set():
    launcher = TtModelLauncher()
    with patch("tt_model_launcher.shutil.which", return_value="/usr/bin/tt-model"), \
         patch("tt_model_launcher.subprocess.Popen") as mock_popen:
        mock_popen.return_value = _fake_popen([])
        launcher.launch(
            TtModelLaunchConfig(bundle_id="acme/llama-fast", port="8001", profile="fast"),
            lambda l: None, lambda s: None,
        )
        launcher._thread.join(timeout=5)

    args = mock_popen.call_args[0][0]
    assert args == ["/usr/bin/tt-model", "serve", "acme/llama-fast", "--port", "8001", "--profile", "fast"]


def test_launch_reports_error_when_tt_model_missing():
    launcher = TtModelLauncher()
    log_lines, states = [], []
    with patch("tt_model_launcher.shutil.which", return_value=None):
        launcher.launch(
            TtModelLaunchConfig(bundle_id="acme/llama-fast", port="8001"),
            log_lines.append, states.append,
        )
        launcher._thread.join(timeout=5)

    assert ServerState.ERROR in states
    assert any("tt-model not found" in l for l in log_lines)


def test_launch_reports_error_on_nonzero_exit():
    launcher = TtModelLauncher()
    states = []
    with patch("tt_model_launcher.shutil.which", return_value="/usr/bin/tt-model"), \
         patch("tt_model_launcher.subprocess.Popen") as mock_popen:
        mock_popen.return_value = _fake_popen(["some error\n"], returncode=1)
        launcher.launch(
            TtModelLaunchConfig(bundle_id="acme/llama-fast", port="8001"),
            lambda l: None, states.append,
        )
        launcher._thread.join(timeout=5)

    assert ServerState.ERROR in states


def test_stop_calls_tt_model_stop_with_bundle_id():
    launcher = TtModelLauncher()
    with patch("tt_model_launcher.shutil.which", return_value="/usr/bin/tt-model"), \
         patch("tt_model_launcher.subprocess.Popen") as mock_popen, \
         patch("tt_model_launcher.subprocess.run") as mock_run:
        mock_popen.return_value = _fake_popen([])
        launcher.launch(
            TtModelLaunchConfig(bundle_id="acme/llama-fast", port="8001"),
            lambda l: None, lambda s: None,
        )
        launcher._thread.join(timeout=5)
        launcher.stop()

    stop_args = mock_run.call_args[0][0]
    assert stop_args == ["/usr/bin/tt-model", "stop", "acme/llama-fast"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/ttuser/code/tt-model-runner && PYTHONPATH=app pytest tests/test_tt_model_launcher.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tt_model_launcher'`

- [ ] **Step 3: Implement**

Create `app/tt_model_launcher.py`:

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Launch a tt-model-manager community bundle via `tt-model serve`.

Unlike ServerManager (which drives run.py inside Docker, with GHCR image
resolution and auto-remediation) `tt-model serve` is a single self-contained
foreground process — no container to poll, no image to resolve. Reuses
ServerManager's LogParser since vllm/tt-dit-server bundles produce the same
startup log patterns run.py's own images do; readiness is left to the
existing HealthWorker (polls the configured port), not detected here.
"""
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from server_manager import LogParser, ServerState


@dataclass
class TtModelLaunchConfig:
    bundle_id: str          # e.g. "episod/tt-animatediff"
    port: str = "8000"
    profile: str = ""       # --profile NAME; empty omits the flag


class TtModelLauncher:
    """Runs `tt-model serve <bundle_id>` and tails its output.

    Public API mirrors ServerManager/DevImageLauncher:
        launch(config, on_log_line, on_state)
        stop()
    """

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._bundle_id: Optional[str] = None
        self.parser = LogParser()

    def launch(self, config: TtModelLaunchConfig,
               on_log_line: Callable[[str], None],
               on_state: Callable[[ServerState], None]) -> None:
        self._stop_event.clear()
        self.parser = LogParser()
        self._bundle_id = config.bundle_id
        self._thread = threading.Thread(
            target=self._run, args=(config, on_log_line, on_state), daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        bundle_id = self._bundle_id
        if bundle_id:
            tt_model_bin = shutil.which("tt-model")
            if tt_model_bin:
                try:
                    subprocess.run(
                        [tt_model_bin, "stop", bundle_id],
                        check=False, capture_output=True, timeout=15,
                    )
                except Exception:
                    pass
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def _run(self, config: TtModelLaunchConfig,
              on_log_line: Callable[[str], None],
              on_state: Callable[[ServerState], None]) -> None:
        tt_model_bin = shutil.which("tt-model")
        if not tt_model_bin:
            on_log_line("✗ tt-model not found — install the tt CLI toolchain")
            on_state(ServerState.ERROR)
            return

        cmd = [tt_model_bin, "serve", config.bundle_id, "--port", str(config.port)]
        if config.profile:
            cmd += ["--profile", config.profile]

        on_log_line(f"▶ {' '.join(cmd)}")
        on_state(ServerState.LAUNCHING)

        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
        except FileNotFoundError:
            on_log_line("✗ tt-model not found — install the tt CLI toolchain")
            on_state(ServerState.ERROR)
            return

        for line in self._proc.stdout:
            if self._stop_event.is_set():
                break
            stripped = line.rstrip("\n")
            on_log_line(stripped)
            new_state = self.parser.feed(stripped)
            if new_state is not None:
                on_state(new_state)

        rc = self._proc.wait()
        if self._stop_event.is_set():
            on_state(ServerState.IDLE)
        elif rc != 0:
            on_log_line(f"✗ tt-model serve exited with code {rc}")
            on_state(ServerState.ERROR)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/ttuser/code/tt-model-runner && PYTHONPATH=app pytest tests/test_tt_model_launcher.py -v`
Expected: PASS (all 5 tests)

- [ ] **Step 5: Commit**

```bash
git add app/tt_model_launcher.py tests/test_tt_model_launcher.py
git commit -m "feat: add TtModelLauncher to serve tt-model-manager bundles"
```

---

### Task 4: Controller wiring — community catalog load + `launch_community()`

**Files:**
- Modify: `app/controller.py`
- Modify: `tests/test_controller_contract.py`
- Test: `tests/test_controller.py`

**Interfaces:**
- Consumes: `community_catalog.load_async`, `tt_model_launcher.TtModelLauncher`, `tt_model_launcher.TtModelLaunchConfig` (Tasks 2–3)
- Produces: `AppController.on_community_catalog_loaded: Optional[Callable]` (`List[ModelEntry]`), `AppController.launch_community(entry: ModelEntry, port: str) -> None`

- [ ] **Step 1: Add `on_community_catalog_loaded` to the contract (failing first)**

In `tests/test_controller_contract.py`, add to `ViewContract`:

```python
    @abstractmethod
    def on_community_catalog_loaded(self, entries: list): ...
```

Add to both `GtkViewStub` and `TuiViewStub`:

```python
    def on_community_catalog_loaded(self, entries): pass
```

Run: `cd /home/ttuser/code/tt-model-runner && PYTHONPATH=app pytest tests/test_controller_contract.py -v`
Expected: FAIL — `test_controller_on_attrs_match_contract` reports the contract has a method (`on_community_catalog_loaded`) the controller doesn't (not yet registered as an instance attribute), and `test_tui_app_registers_all_callbacks`/`test_gtk_app_registers_all_callbacks` report it's not registered in `app/tui/app.py`/`app/main_window.py` either.

- [ ] **Step 2: Wire the callback and background load into `AppController.__init__`**

In `app/controller.py`, add the import near the other catalog/launcher imports (after the `dev_image_launcher` import, `app/controller.py:26`):

```python
from community_catalog import load_async as _community_load_async
from tt_model_launcher import TtModelLauncher, TtModelLaunchConfig
```

In `__init__`, alongside `self._dev_launcher = DevImageLauncher()` (`app/controller.py:314`), add:

```python
        self._tt_model_launcher = TtModelLauncher()
        self._community_entries: List[ModelEntry] = []
```

Alongside the compat-catalog background fetch (`app/controller.py:328-332`), add:

```python
        # Fetch tt-model-manager community bundles in the background —
        # merges into self._catalog once loaded (or immediately if the
        # catalog is already loaded) and dispatches on_community_catalog_loaded.
        def _on_community(entries: List[ModelEntry]) -> None:
            self._community_entries = entries
            if self._catalog is not None and entries:
                self._catalog.merge_community(entries)
                self._emit("on_catalog_loaded", self._catalog, self._last_compatible_devices)
            self._emit("on_community_catalog_loaded", entries)
        _community_load_async(_on_community)
```

This references `self._last_compatible_devices`, which doesn't exist yet —
add it right above (`self._catalog: Optional[ModelCatalog] = None` is at
`app/controller.py:301`):

```python
        self._catalog: Optional[ModelCatalog] = None
        self._last_compatible_devices: List[str] = []
```

And set it in `load_repo` (`app/controller.py:415-423`) right before the
existing `self._emit("on_catalog_loaded", ...)` call:

```python
            compatible = devices if devices else self._catalog.all_device_types()
            if not devices:
                self._emit("on_log_line", "⚠ tt-smi not found — showing all devices")
            self._last_compatible_devices = compatible
            if self._community_entries:
                self._catalog.merge_community(self._community_entries)
            self._emit("on_catalog_loaded", self._catalog, compatible)
```

Add the callback declaration next to the others (`app/controller.py:346`,
after `on_compat_catalog_loaded`):

```python
        self.on_community_catalog_loaded: Optional[Callable] = None    # (List[ModelEntry],)
```

- [ ] **Step 3: Add `launch_community()` and wire `stop()`**

Add near `launch_dev_image` (`app/controller.py:1068-1087`):

```python
    def launch_community(self, entry: ModelEntry, port: str) -> None:
        """Launch a tt-model-manager community bundle via `tt-model serve`.

        entry.source must be "community"; entry.model_id is the bundle id
        (e.g. "org/name"). No LaunchOptions, no GHCR resolution, no
        auto-remediation — see docs/superpowers/specs/2026-09-17-tt-cli-community-models-design.md.
        """
        if self._state not in (ServerState.IDLE, ServerState.ERROR):
            return
        self._current_entry = entry
        self._port = port
        config = TtModelLaunchConfig(bundle_id=entry.model_id, port=port)
        self._emit("on_log_line", f"▶ Launching community bundle {entry.model_id} · port {port}")
        self._transition(ServerState.LAUNCHING)

        self._last_health_port = port
        self._last_health_engine = "auto"
        self._health_worker = HealthWorker(
            port=port,
            on_ready=self._on_health_ready,
            on_lost=self._on_health_lost,
            dispatch_fn=self._dispatch,
            engine="auto",
        )
        self._health_worker.start()
        self._tt_model_launcher.launch(config, self._handle_log_line, self._on_server_state)
```

Modify `stop()` (`app/controller.py:589-597`) to also stop the tt-model
launcher:

```python
    def stop(self) -> None:
        """Stop the running server, dev-image script, or community bundle."""
        self._transition(ServerState.STOPPING)
        if self._health_worker:
            self._health_worker.stop()
            self._health_worker = None
        self._server_mgr.stop()
        self._dev_launcher.stop()
        self._tt_model_launcher.stop()
        t = threading.Timer(10.0, self._force_idle)
        t.daemon = True
        t.start()
```

- [ ] **Step 4: Register the callback in both views**

In `app/tui/app.py`, find where other `on_*` callbacks are assigned to
`self._ctrl` (near the compat/catalog registrations) and add:

```python
        self._ctrl.on_community_catalog_loaded = self._on_community_catalog_loaded
```

Add the handler method (near `_on_compat_catalog_loaded`-equivalent logic,
or if none exists in the TUI yet, near `action_launch_stop`):

```python
    def _on_community_catalog_loaded(self, entries: list) -> None:
        rail = self.query_one(ModelRail)
        rail.load_community_entries(entries)
```

(`ModelRail.load_community_entries` is added in Task 6 — this reference is
expected to be unresolved until then; that's fine, it's the next task.)

In `app/main_window.py`, find where `controller.on_compat_catalog_loaded = ...`
is assigned (`app/main_window.py:3174` area) and add:

```python
        controller.on_community_catalog_loaded = self._on_community_catalog_loaded
```

Add the handler (near `_on_compat_catalog_loaded`, `app/main_window.py:3560`):

```python
    def _on_community_catalog_loaded(self, entries: list) -> None:
        """Pass freshly-fetched tt-model-manager bundles to the sidebar."""
        self._sidebar.load_community_entries(entries)
```

(`Sidebar.load_community_entries` is added in Task 7.)

- [ ] **Step 5: Run contract tests to verify they pass**

Run: `cd /home/ttuser/code/tt-model-runner && PYTHONPATH=app pytest tests/test_controller_contract.py -v`
Expected: PASS — all four contract tests green (the `_parse_registered_callbacks`
AST scan in `test_tui_app_registers_all_callbacks`/`test_gtk_app_registers_all_callbacks`
only checks for the `self._ctrl.on_X = ...` / `controller.on_X = ...`
assignment, not that the referenced handler method resolves — so this test
passes even though `ModelRail.load_community_entries`/`Sidebar.load_community_entries`
don't exist until Tasks 6–7. `app/tui/app.py` and `app/main_window.py` are
still valid Python at this point since the handler methods themselves
(`_on_community_catalog_loaded`) ARE defined — they just call methods on
`ModelRail`/`Sidebar` that raise `AttributeError` only if actually invoked
at runtime, which the contract test doesn't do.)

- [ ] **Step 6: Add a controller-level test for `launch_community`**

Add to `tests/test_controller.py` (check its existing `NullDispatch`/fixture
pattern first and match it — the shape below assumes a `make_controller()`
helper or direct `AppController()` construction with a synchronous dispatch,
consistent with the rest of that file):

```python
def test_launch_community_uses_tt_model_launcher(monkeypatch):
    ctrl = AppController()  # or the file's existing test-construction helper
    from model_catalog import ModelEntry
    entry = ModelEntry(
        model_id="org/bundle", model_name="org/bundle", display_name="bundle",
        hf_model_repo="org/weights", model_type="COMMUNITY", family="org",
        device_type="UNKNOWN", inference_engine="vllm", docker_image="",
        status="COMMUNITY", param_count=None, min_disk_gb=None, min_ram_gb=None,
        source="community",
    )
    launched = {}
    def _fake_launch(config, on_log_line, on_state):
        launched["bundle_id"] = config.bundle_id
        launched["port"] = config.port
    monkeypatch.setattr(ctrl._tt_model_launcher, "launch", _fake_launch)
    ctrl.launch_community(entry, "8001")
    assert launched == {"bundle_id": "org/bundle", "port": "8001"}
```

Adjust construction to match whatever `AppController()` requires in this
test file already (e.g. a `dispatch_fn=lambda fn, *a: fn(*a)` argument) —
read `tests/test_controller.py`'s existing tests for the exact pattern
before writing this one in.

Run: `cd /home/ttuser/code/tt-model-runner && PYTHONPATH=app pytest tests/test_controller.py tests/test_controller_contract.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/controller.py app/tui/app.py app/main_window.py tests/test_controller_contract.py tests/test_controller.py
git commit -m "feat: wire community catalog loading and launch_community into AppController"
```

---

### Task 5: TUI — "Community" section in `ModelRail`

**Files:**
- Modify: `app/tui/widgets/model_rail.py`
- Modify: `app/tui/app.py`

**Interfaces:**
- Consumes: `AppController.launch_community` (Task 4), `ModelEntry.source` (Task 1)
- Produces: `ModelRail.load_community_entries(entries: list) -> None`

- [ ] **Step 1: Add the Community list to `compose()`**

In `app/tui/widgets/model_rail.py`, add a new section after the `#discover-list`
block in `compose()` (`app/tui/widgets/model_rail.py:150-152`):

```python
        yield Static("", id="discover-label")
        yield ListView(id="discover-list")
        yield Static("", id="community-label")
        yield ListView(id="community-list")
        yield Static("", id="hw-strip", markup=True)
```

Add matching CSS to `DEFAULT_CSS` (next to `#discover-list`'s rule,
`app/tui/widgets/model_rail.py:87-89`):

```python
    #community-list {
        max-height: 6;
    }
```

- [ ] **Step 2: Add `load_community_entries()` and route selection**

Add a new instance list in `__init__` (`app/tui/widgets/model_rail.py:129-136`,
next to `self._compat_entries`):

```python
        self._community_entries_raw: list = []
```

Add the loader method (near `load_compat_catalog`,
`app/tui/widgets/model_rail.py:281-310`):

```python
    def load_community_entries(self, entries: list) -> None:
        """Populate the COMMUNITY section from tt-model-manager bundles."""
        self._community_entries_raw = list(entries)
        clv = self.query_one("#community-list", ListView)
        clv.clear()
        lbl = self.query_one("#community-label", Static)
        if entries:
            lbl.update(f"[dim]— COMMUNITY ({len(entries)}) —[/dim]")
            for e in entries:
                item = ListItem(Label(f"{e.display_name[:18]}\n  [{e.device_type}]"))
                item._entry = e
                item._compat_entry = None
                clv.append(item)
        else:
            lbl.update("")
```

`on_list_view_selected` (`app/tui/widgets/model_rail.py:345-356`) already
routes any `ListItem` with `_entry` set through `self.on_model_select` —
no change needed there; community entries flow through the same path as
catalog entries, distinguished later by `entry.source`.

- [ ] **Step 3: Route launch based on `entry.source` in `TuiApp`**

In `app/tui/app.py`, modify `_do_launch_from_rail` and `_do_launch`
(`app/tui/app.py:318-332`):

```python
    def _do_launch_from_rail(self) -> None:
        rail = self.query_one(ModelRail)
        entry = rail.selected_entry
        port  = rail.port_value
        if not entry:
            return
        if getattr(entry, "source", "inference_server") == "community":
            self._ctrl.launch_community(entry, port)
        else:
            opts = self._ctrl.get_options()
            self._ctrl.launch(entry, port, opts)

    def _do_launch(self, entry, port: str) -> None:
        if getattr(entry, "source", "inference_server") == "community":
            self._ctrl.launch_community(entry, port)
        else:
            opts = self._ctrl.get_options()
            self._ctrl.launch(entry, port, opts)
        # Refresh RECENT section after launch so the new entry appears immediately.
        self.call_after_refresh(
            lambda: self.query_one(ModelRail)._refresh_starred_recent()
        )
```

- [ ] **Step 4: Manual verification (no automated widget tests exist for TUI panes today)**

Run: `cd /home/ttuser/code/tt-model-runner && ./run --tui`

Expected: the app launches without exceptions; the sidebar shows a
"COMMUNITY" section (empty if `tt` isn't installed, or listing bundles if
it is — verify with whichever is true on this machine). Selecting a
community entry and pressing `L` should invoke `launch_community` (check
the log pane for the `▶ Launching community bundle ...` line from Task 4's
`launch_community`). Press `q` to quit.

Run the existing test suite to confirm nothing regressed:
`PYTHONPATH=app pytest tests/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/tui/widgets/model_rail.py app/tui/app.py
git commit -m "feat(tui): add Community section to ModelRail sidebar"
```

---

### Task 6: GTK — "Community" section in `Sidebar`

**Files:**
- Modify: `app/main_window.py`

**Interfaces:**
- Consumes: `AppController.launch_community` (Task 4), `ModelEntry.source` (Task 1)
- Produces: `Sidebar.load_community_entries(entries: list) -> None`

- [ ] **Step 1: Add `load_community_entries()` to `Sidebar`, appended after the tree**

In `app/main_window.py`, add an instance attribute in `Sidebar.__init__`
(near wherever `self._catalog`/`self._compat_catalog` are initialized —
check the constructor above `_build` at `app/main_window.py:182`):

```python
        self._community_entries: list = []
```

Add a new method near `_rebuild_tree` (`app/main_window.py:921-1072`):

```python
    def load_community_entries(self, entries: list) -> None:
        """Store community bundles and re-render the tree to include them."""
        self._community_entries = list(entries)
        if self._selected_device:
            self._rebuild_tree([self._selected_device])
        else:
            self._rebuild_tree(None)
```

- [ ] **Step 2: Render the Community section at the end of `_rebuild_tree`**

At the end of `_rebuild_tree` (`app/main_window.py`, right after the
existing "DISCOVER section" block, `app/main_window.py:1070-1072`), add:

```python
        # COMMUNITY section — tt-model-manager bundles, always shown (not
        # gated by device compatibility filtering like the main tree).
        if self._community_entries:
            search = getattr(self, "_search_filter", "")
            visible = self._community_entries
            if search:
                visible = [
                    e for e in visible
                    if search in e.display_name.lower() or search in (e.family or "").lower()
                ]
            if visible:
                comm_it = self._tree_store.append(
                    None, [f"COMMUNITY ({len(visible)})", "", "", False]
                )
                for entry in visible:
                    label = f"{entry.display_name}  [{entry.device_type}]"
                    self._tree_store.append(
                        comm_it, [label, entry.model_name, entry.device_type, True]
                    )
```

This reuses the same `TreeStore` shape (`label, model_name, device_type,
is_leaf`) the rest of `_rebuild_tree` already uses, so the existing
selection-handling code (whatever reads `tree_store` rows on click) picks
up community rows without any further change — verify this in Step 4.

- [ ] **Step 3: Route launch based on `entry.source` in `MainWindow`**

Modify `_on_launch_clicked` (`app/main_window.py:3568-3577`):

```python
    def _on_launch_clicked(self, entry: ModelEntry, port: str) -> None:
        """Collect current options from the config panel and ask the controller
        to start the server.  If the engine family changed since the last launch,
        show a dialog recommending tt-smi -r first."""
        if getattr(entry, "source", "inference_server") == "community":
            self._ctrl.launch_community(entry, port)
            return
        warning = self._ctrl.needs_reset_warning(entry)
        if warning:
            old_engine, new_engine, old_model = warning
            self._show_reset_warning_dialog(entry, port, old_engine, new_engine, old_model)
        else:
            self._ctrl.launch(entry, port, self._panel.get_options())
```

(Community bundles skip the reset-warning dialog entirely — that logic is
keyed on inference-engine changes tracked for the docker/run.py path, which
doesn't apply here.)

- [ ] **Step 4: Manual verification**

Run: `cd /home/ttuser/code/tt-model-runner && ./run`

Expected: the app launches without exceptions; the sidebar's tree view
shows a "COMMUNITY (N)" top-level row (N may be 0 if `tt` isn't installed
on this machine — still verify the row doesn't appear at all when N is 0,
per the `if visible:` guard in Step 2). Selecting a community leaf and
clicking the model click-handler that populates the config panel/selection
state should work the same as any other leaf (verify by checking whatever
existing "selection changed" handler reads `model_name`/`device_type` from
the tree row — locate it via `grep -n "get_selection\|row-activated\|cursor-changed" app/main_window.py`
if unclear, and confirm it doesn't assume `device_type` is a known
`model_spec.json` value anywhere that would break on `"UNKNOWN"`).
Selecting and launching a community entry should log the
`▶ Launching community bundle ...` line.

Run the existing test suite to confirm nothing regressed:
`PYTHONPATH=app pytest tests/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/main_window.py
git commit -m "feat(gtk): add Community section to Sidebar tree"
```

---

### Task 7: `--dev-mode` + `--docker-server` requires `--override-docker-image`

**Files:**
- Modify: `app/server_manager.py:388-396` (`ServerManager.launch`, where
  `_docker_img` is resolved and `--docker-server`/`--dev-mode` are both
  already known — confirmed by reading this method: `--docker-server` is
  unconditionally in `cmd` at line 381, and `config.options.dev_mode` /
  `_docker_img` are both in scope right after line 394)
- Test: `tests/test_server_manager.py`

**Interfaces:**
- Consumes: `server_manager.LaunchConfig`, `server_manager.ServerState` (existing)
- Produces: no new public interface — `ServerManager.launch()` now calls
  `on_state(ServerState.ERROR)` and returns before spawning a process when
  the guard trips, following the same callback-based (not exception-based)
  error-reporting convention already used throughout this class.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_server_manager.py`:

```python
from unittest.mock import patch
from launch_options import LaunchOptions
from server_manager import LaunchConfig, ServerManager


def test_launch_errors_when_dev_mode_without_docker_image_override(tmp_path):
    mgr = ServerManager()
    config = LaunchConfig(
        repo_path=tmp_path,
        model_name="test-model",
        device="N150",
        port="8000",
        options=LaunchOptions(dev_mode=True, docker_image_override=""),
    )
    log_lines, states = [], []
    with patch("server_manager.subprocess.Popen") as mock_popen:
        mgr.launch(config, log_lines.append, states.append)

    mock_popen.assert_not_called()
    assert ServerState.ERROR in states
    assert any("--dev-mode" in l and "--override-docker-image" in l for l in log_lines)


def test_launch_allows_dev_mode_with_docker_image_override(tmp_path):
    mgr = ServerManager()
    config = LaunchConfig(
        repo_path=tmp_path,
        model_name="test-model",
        device="N150",
        port="8000",
        options=LaunchOptions(dev_mode=True, docker_image_override="ghcr.io/tt/dev:latest"),
    )
    states = []
    with patch("server_manager.subprocess.Popen") as mock_popen:
        mock_popen.return_value.stdout = iter([])
        mgr.launch(config, lambda l: None, states.append)

    mock_popen.assert_called_once()
    assert ServerState.ERROR not in states
```

- [ ] **Step 2: Run tests to verify the first one fails**

Run: `cd /home/ttuser/code/tt-model-runner && PYTHONPATH=app pytest tests/test_server_manager.py -k dev_mode -v`
Expected: `test_launch_errors_when_dev_mode_without_docker_image_override` FAILS
(`mock_popen.assert_not_called()` fails — today `Popen` is called regardless);
`test_launch_allows_dev_mode_with_docker_image_override` already PASSES
(existing behavior doesn't block this case, nothing to fix there).

- [ ] **Step 3: Implement the guard**

In `app/server_manager.py`, right after `_docker_img` is computed
(`app/server_manager.py:391-396`):

```python
        _docker_img = (
            (config.options.docker_image_override if config.options else "")
            or config.docker_image_override
        )
        if config.options and config.options.dev_mode and not _docker_img:
            on_log_line(
                "✗ --dev-mode requires --override-docker-image when using "
                "--docker-server — dev specs do not pin a docker image"
            )
            on_state(ServerState.ERROR)
            return
        if _docker_img:
            cmd += ["--override-docker-image", _docker_img]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/ttuser/code/tt-model-runner && PYTHONPATH=app pytest tests/test_server_manager.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add app/server_manager.py tests/test_server_manager.py
git commit -m "fix: require --override-docker-image when --dev-mode is used with --docker-server"
```

---

## Final verification

- [ ] Run the full suite: `cd /home/ttuser/code/tt-model-runner && PYTHONPATH=app pytest tests/ -v` — expect all green.
- [ ] Manually launch both `./run` and `./run --tui`, confirm the Community
      section renders (empty-state hint if `tt`/`tt-model` aren't installed
      on this machine, populated if they are), and that the existing
      tt-inference-server launch flow is unaffected (launch a known-good
      model end to end if hardware is available — take a `gozer` lease
      first per this machine's global CLAUDE.md).
