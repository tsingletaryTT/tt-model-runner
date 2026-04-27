# Local Favorites / Last-Successful-Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pre-fill the last successful launch config on startup, let users save/load/delete named configs, and show a "Copy env vars" button when a server is READY.

**Architecture:** New `SavedConfig` dataclass + `SavedConfigStore` module (modeled on `deploy_profile_store.py`). AppController writes last-success on READY; ProfileOrchestrator writes when all slots READY. MainWindow reads on startup and provides a Saved Configs section in the sidebar plus an env-vars popover when READY.

**Tech Stack:** Python 3.10+, GTK 4 (PyGObject), dataclasses, json, pathlib, pytest

---

## File Map

| File | Action | What changes |
|------|--------|-------------|
| `app/saved_config.py` | Create | `SavedConfig` dataclass |
| `app/saved_config_store.py` | Create | CRUD: `save_last_success`, `load_last_success`, `save_named`, `load_named`, `list_named`, `delete_named` |
| `tests/test_saved_config_store.py` | Create | Store unit tests (monkeypatched tmp dir) |
| `app/controller.py` | Modify | Store `_last_launch_config`; write last-success in `_on_health_ready` |
| `app/profile_orchestrator.py` | Modify | Write last-success after all slots READY |
| `app/deploy_panel.py` | Modify | Add `select_profile(name)` public method |
| `app/main_window.py` | Modify | `_restore_config`; Saved Configs section in `Sidebar._build`; `set_env_context`/`set_env_visible`; env popover; call restore in `MainWindow.__init__` |
| `tests/test_saved_config_store.py` | Create | Already listed above |

---

## Task 1: SavedConfig dataclass

**Files:**
- Create: `app/saved_config.py`
- Test: `tests/test_saved_config_store.py` (partial — dataclass tests)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_saved_config_store.py
import json, dataclasses, pytest
from pathlib import Path

# ── Task 1 tests ──────────────────────────────────────────────────────────────

def test_saved_config_roundtrip_json():
    """SavedConfig serialises and deserialises through json without data loss."""
    import sys; sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
    from saved_config import SavedConfig
    cfg = SavedConfig(
        name="test",
        model_name="Llama-3.1-8B",
        device_type="n300",
        port=8000,
        docker_image="ghcr.io/tenstorrent/tt-inference-server:v0.0.1",
        inference_engine="vllm",
        options_json='{"use_case": "chat"}',
        deploy_profile_name="",
        created="2026-04-27T12:00:00",
        last_used="2026-04-27T12:05:00",
    )
    data = dataclasses.asdict(cfg)
    restored = SavedConfig(**data)
    assert restored == cfg
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd /home/ttuser/code/tt-model-runner
PYTHONPATH=app pytest tests/test_saved_config_store.py::test_saved_config_roundtrip_json -v
```

Expected: `ModuleNotFoundError: No module named 'saved_config'`

- [ ] **Step 3: Create `app/saved_config.py`**

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""SavedConfig dataclass — snapshot of a successful launch configuration."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class SavedConfig:
    name: str               # "__last_success__" or user-chosen name
    model_name: str         # display name from catalog ("" for profile-only)
    device_type: str        # e.g. "n300" ("" for profile-only)
    port: int               # 0 for profile-only
    docker_image: str       # full tag or "" for profile-only
    inference_engine: str   # "vllm" | "tt-transformers" | ""
    options_json: str       # json.dumps(dataclasses.asdict(LaunchOptions))
    deploy_profile_name: str  # "" for single-server; profile name for multi-slot
    created: str            # ISO timestamp, set on first save
    last_used: str          # ISO timestamp, updated on every restore
```

- [ ] **Step 4: Run to confirm pass**

```bash
PYTHONPATH=app pytest tests/test_saved_config_store.py::test_saved_config_roundtrip_json -v
```

Expected: `PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/saved_config.py tests/test_saved_config_store.py
git commit -m "feat: add SavedConfig dataclass"
```

---

## Task 2: SavedConfigStore

**Files:**
- Create: `app/saved_config_store.py`
- Modify: `tests/test_saved_config_store.py` (add store tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_saved_config_store.py`:

```python
# ── Task 2 tests ──────────────────────────────────────────────────────────────

import saved_config_store as scs
from saved_config import SavedConfig


def _make_cfg(name="test", profile="") -> SavedConfig:
    return SavedConfig(
        name=name, model_name="Llama", device_type="n300", port=8000,
        docker_image="ghcr.io/tt/img:v1", inference_engine="vllm",
        options_json='{"use_case":"chat"}',
        deploy_profile_name=profile, created="", last_used="",
    )


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(scs, "_SAVED_CONFIGS_DIR", tmp_path)
    return tmp_path


def test_save_and_load_last_success():
    cfg = _make_cfg("__last_success__")
    scs.save_last_success(cfg)
    loaded = scs.load_last_success()
    assert loaded is not None
    assert loaded.name == "__last_success__"
    assert loaded.model_name == "Llama"


def test_load_last_success_returns_none_when_absent():
    assert scs.load_last_success() is None


def test_save_named_and_list():
    scs.save_named(_make_cfg("my-config"))
    scs.save_named(_make_cfg("another-config"))
    names = scs.list_named()
    assert names == ["another-config", "my-config"]


def test_list_named_excludes_last_success(tmp_path):
    scs.save_last_success(_make_cfg("__last_success__"))
    scs.save_named(_make_cfg("named"))
    assert scs.list_named() == ["named"]


def test_load_named_roundtrip():
    scs.save_named(_make_cfg("roundtrip"))
    loaded = scs.load_named("roundtrip")
    assert loaded.device_type == "n300"
    assert loaded.name == "roundtrip"


def test_delete_named():
    scs.save_named(_make_cfg("todelete"))
    scs.delete_named("todelete")
    assert "todelete" not in scs.list_named()


def test_delete_last_success_raises():
    with pytest.raises(ValueError, match="__last_success__"):
        scs.delete_named("__last_success__")


def test_save_named_rejects_sentinel():
    with pytest.raises(ValueError, match="__last_success__"):
        scs.save_named(_make_cfg("__last_success__"))


def test_path_traversal_guard():
    with pytest.raises(ValueError, match="Invalid"):
        scs.save_named(_make_cfg("../evil"))


def test_created_timestamp_set_on_first_save():
    cfg = _make_cfg("ts-test")
    assert cfg.created == ""
    scs.save_named(cfg)
    loaded = scs.load_named("ts-test")
    assert loaded.created != ""


def test_atomic_write(tmp_path):
    """File is written via tmp+rename so a partial write is never observed."""
    cfg = _make_cfg("atomic")
    scs.save_named(cfg)
    # Verify no leftover .tmp file
    assert not list(tmp_path.glob("*.tmp"))
```

- [ ] **Step 2: Run to confirm failures**

```bash
PYTHONPATH=app pytest tests/test_saved_config_store.py -v -k "not roundtrip_json"
```

Expected: All fail with `ModuleNotFoundError: No module named 'saved_config_store'`

- [ ] **Step 3: Create `app/saved_config_store.py`**

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Persist SavedConfig to ~/.config/tt-runner-gui/saved_configs/.

Public API
----------
save_last_success(config)  -- overwrite __last_success__.json
load_last_success()        -- return SavedConfig or None
save_named(config)         -- write <name>.json (config.name must not be sentinel)
load_named(name)           -- return SavedConfig; raises FileNotFoundError if absent
list_named()               -- sorted list of names (excludes __last_success__)
delete_named(name)         -- remove <name>.json; raises ValueError for sentinel
"""
import json
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from saved_config import SavedConfig

# Module-level constant; monkeypatched in tests via scs._SAVED_CONFIGS_DIR.
_SAVED_CONFIGS_DIR = Path.home() / ".config" / "tt-runner-gui" / "saved_configs"
_SENTINEL = "__last_success__"
_NAME_RE = re.compile(r"^[\w\-. ]+$")


def _config_path(name: str) -> Path:
    """Return the absolute path for a config JSON file with path-traversal guard."""
    base = _SAVED_CONFIGS_DIR
    candidate = (base / f"{name}.json").resolve()
    if base.resolve() not in candidate.parents:
        raise ValueError(f"Invalid config name: {name!r}")
    return base / f"{name}.json"


def _dict_to_config(data: dict) -> SavedConfig:
    """Deserialise a raw dict into SavedConfig, ignoring unknown keys."""
    known = set(SavedConfig.__dataclass_fields__)
    return SavedConfig(**{k: v for k, v in data.items() if k in known})


def _write(path: Path, data: dict) -> None:
    """Atomic write: write to .tmp then rename."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(path)


def save_last_success(config: SavedConfig) -> None:
    """Overwrite __last_success__.json with *config*."""
    _SAVED_CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    data = asdict(config)
    data["name"] = _SENTINEL
    now = datetime.now().isoformat(timespec="seconds")
    if not data["created"]:
        data["created"] = now
    data["last_used"] = now
    _write(_config_path(_SENTINEL), data)


def load_last_success() -> Optional[SavedConfig]:
    """Return the last-success config, or None if no successful launch yet."""
    path = _config_path(_SENTINEL)
    if not path.exists():
        return None
    try:
        return _dict_to_config(json.loads(path.read_text()))
    except (json.JSONDecodeError, OSError, TypeError, KeyError):
        return None


def save_named(config: SavedConfig) -> None:
    """Write a user-named config to disk.

    Raises ValueError if name is the sentinel or fails the name regex.
    """
    name = config.name
    if name == _SENTINEL:
        raise ValueError(f"Cannot save with reserved name {_SENTINEL!r}")
    if not _NAME_RE.match(name):
        raise ValueError(f"Invalid config name: {name!r}")
    _SAVED_CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    data = asdict(config)
    now = datetime.now().isoformat(timespec="seconds")
    if not data["created"]:
        data["created"] = now
    data["last_used"] = now
    _write(_config_path(name), data)


def load_named(name: str) -> SavedConfig:
    """Return the named config; raises FileNotFoundError if absent."""
    path = _config_path(name)
    if not path.exists():
        raise FileNotFoundError(f"No saved config named {name!r}")
    return _dict_to_config(json.loads(path.read_text()))


def list_named() -> list:
    """Return sorted list of user-named config names (excludes __last_success__)."""
    if not _SAVED_CONFIGS_DIR.exists():
        return []
    return sorted(
        p.stem for p in _SAVED_CONFIGS_DIR.glob("*.json")
        if p.stem != _SENTINEL
    )


def delete_named(name: str) -> None:
    """Delete a named config file; raises ValueError for the sentinel name."""
    if name == _SENTINEL:
        raise ValueError(f"Cannot delete reserved config {_SENTINEL!r}")
    path = _config_path(name)
    if path.exists():
        path.unlink()
```

- [ ] **Step 4: Run to confirm all pass**

```bash
PYTHONPATH=app pytest tests/test_saved_config_store.py -v
```

Expected: All 12 tests `PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/saved_config_store.py tests/test_saved_config_store.py
git commit -m "feat: add SavedConfigStore with save/load/list/delete"
```

---

## Task 3: AppController READY hook

Write last-success to disk when the single-server HealthWorker reports READY.

**Files:**
- Modify: `app/controller.py`
- Modify: `tests/test_controller.py` (add READY hook test)

**Background:** `_on_health_ready` (line ~1019) fires when the server health endpoint returns 200. `LaunchConfig` (which holds `docker_image_override`) is a local in `_do_launch` — store it on `self` so `_on_health_ready` can read it.

- [ ] **Step 1: Write the failing test**

Open `tests/test_controller.py`. Append the following test near the end of the file (before any `if __name__` guard if present):

```python
def test_ready_writes_last_success(tmp_path, monkeypatch):
    """On READY, AppController writes a last-success SavedConfig."""
    import saved_config_store as scs
    monkeypatch.setattr(scs, "_SAVED_CONFIGS_DIR", tmp_path)

    import sys; sys.path.insert(0, "app")
    from controller import AppController
    from server_manager import ServerState

    ctrl = AppController(dispatch_fn=lambda fn, *a: fn(*a))

    # Simulate a completed launch: set the internal state that _on_health_ready reads.
    from model_catalog import ModelEntry
    entry = ModelEntry(
        model_name="llama-3-8b", display_name="Llama 3 8B",
        device_type="n300", hf_model_repo="meta-llama/Llama-3-8B",
        inference_engine="vllm", docker_image="",
        param_count=None, model_type="LLM", family="Llama",
        max_context=None, perf_reference=None,
    )
    ctrl._current_entry = entry
    ctrl._state = ServerState.LOADING

    from server_manager import LaunchConfig
    from pathlib import Path
    ctrl._last_launch_config = LaunchConfig(
        repo_path=Path("/tmp"), model_name="meta-llama/Llama-3-8B",
        device="n300", port="8000",
        docker_image_override="ghcr.io/tt/img:v1",
    )

    ctrl._on_health_ready(["meta-llama/Llama-3-8B"])

    loaded = scs.load_last_success()
    assert loaded is not None
    assert loaded.model_name == "Llama 3 8B"
    assert loaded.device_type == "n300"
    assert loaded.port == 8000
    assert loaded.docker_image == "ghcr.io/tt/img:v1"
    assert loaded.deploy_profile_name == ""
```

- [ ] **Step 2: Run to confirm failure**

```bash
PYTHONPATH=app pytest tests/test_controller.py::test_ready_writes_last_success -v
```

Expected: `FAILED` — `AttributeError: 'AppController' object has no attribute '_last_launch_config'`

- [ ] **Step 3: Add `_last_launch_config = None` to `AppController.__init__`**

In `app/controller.py`, find the block of instance variable initialisations (around line 280, near `self._last_health_port`). Add after the `_last_health_engine` line:

```python
        self._last_launch_config = None   # set by _do_launch; read by _on_health_ready
```

- [ ] **Step 4: Store config in `_do_launch`**

In `app/controller.py`, find `_do_launch` where `config = build_launch_config(...)` is called (around line 496–507). After the `config = build_launch_config(...)` call block (right before `if not config.hf_token:`), add:

```python
        self._last_launch_config = config
```

- [ ] **Step 5: Write last-success in `_on_health_ready`**

In `app/controller.py`, find `_on_health_ready` (around line 1019). After the `self._transition(ServerState.READY, ...)` call (and the timing block that follows it), add:

```python
            # Persist last-success config for restore-on-startup.
            if self._current_entry and self._last_launch_config:
                try:
                    import json
                    import dataclasses as _dc
                    from saved_config import SavedConfig
                    from saved_config_store import save_last_success
                    save_last_success(SavedConfig(
                        name="__last_success__",
                        model_name=self._current_entry.display_name,
                        device_type=self._current_entry.device_type,
                        port=int(self._last_launch_config.port),
                        docker_image=self._last_launch_config.docker_image_override,
                        inference_engine=self._current_entry.inference_engine,
                        options_json=json.dumps(_dc.asdict(self._options)),
                        deploy_profile_name="",
                        created="",
                        last_used="",
                    ))
                except Exception:
                    pass  # never crash the health callback over a save failure
```

- [ ] **Step 6: Run to confirm pass**

```bash
PYTHONPATH=app pytest tests/test_controller.py::test_ready_writes_last_success -v
```

Expected: `PASSED`

- [ ] **Step 7: Run full test suite**

```bash
PYTHONPATH=app pytest tests/ -v --tb=short 2>&1 | tail -20
```

Expected: All existing tests still pass.

- [ ] **Step 8: Commit**

```bash
git add app/controller.py tests/test_controller.py
git commit -m "feat: write last-success SavedConfig on AppController READY"
```

---

## Task 4: ProfileOrchestrator READY hook

Write last-success when all profile slots reach READY.

**Files:**
- Modify: `app/profile_orchestrator.py`
- Modify: `tests/test_profile_orchestrator.py`

- [ ] **Step 1: Write the failing test**

Open `tests/test_profile_orchestrator.py`. Append near the end:

```python
def test_all_slots_ready_writes_last_success(tmp_path, monkeypatch):
    """ProfileOrchestrator writes a last-success entry when all slots are READY."""
    import sys; sys.path.insert(0, "app")
    import saved_config_store as scs
    monkeypatch.setattr(scs, "_SAVED_CONFIGS_DIR", tmp_path)

    from deploy_profile import DeployProfile, ProfileSlot, OrchestratorCallbacks
    from launch_options import LaunchOptions
    from profile_orchestrator import ProfileOrchestrator
    from server_manager import ServerState
    from unittest.mock import MagicMock, patch

    cbs = OrchestratorCallbacks(
        on_slot_state=lambda *a: None,
        on_slot_log=lambda *a: None,
        on_slot_progress=lambda *a: None,
        on_profile_done=lambda: None,
    )

    orch = ProfileOrchestrator(
        settings=MagicMock(server_repo_path="/tmp", cache_root_path="", hf_token=""),
        dispatch_fn=lambda fn, *a: fn(*a),
        callbacks=cbs,
    )

    profile = DeployProfile(
        name="my-profile",
        slots=[ProfileSlot(
            model_name="Llama-3.1-8B", device_type="n300",
            port="8001", options=LaunchOptions(),
        )],
        created="",
    )

    # Patch _run_sequential to simulate all-READY without real Docker
    from deploy_profile import SlotRunState
    from server_manager import ServerState as SS

    rs = SlotRunState(
        slot=profile.slots[0], state=SS.READY,
        server_mgr=MagicMock(), health_worker=MagicMock(),
        log_lines=[], port="8001", chip_index=0,
    )

    def fake_sequential(p, catalog, chips):
        orch._slot_states = [rs]
        # Simulate all-READY path: write last success then dispatch done
        orch._write_last_success_if_all_ready(p)
        orch._dispatch(orch._cbs.on_profile_done)

    orch._run_sequential = fake_sequential
    orch.launch_profile(profile, MagicMock())
    import time; time.sleep(0.1)  # let thread finish

    loaded = scs.load_last_success()
    assert loaded is not None
    assert loaded.deploy_profile_name == "my-profile"
    assert loaded.model_name == ""
```

- [ ] **Step 2: Run to confirm failure**

```bash
PYTHONPATH=app pytest tests/test_profile_orchestrator.py::test_all_slots_ready_writes_last_success -v
```

Expected: `FAILED` — `AttributeError: 'ProfileOrchestrator' object has no attribute '_write_last_success_if_all_ready'`

- [ ] **Step 3: Add `_write_last_success_if_all_ready` to `ProfileOrchestrator`**

In `app/profile_orchestrator.py`, add this method to the `ProfileOrchestrator` class (just before `_run_sequential`):

```python
    def _write_last_success_if_all_ready(self, profile: DeployProfile) -> None:
        """Write a last-success SavedConfig if every slot reached READY."""
        from server_manager import ServerState as SS
        all_ready = (
            self._slot_states
            and all(s is not None and s.state == SS.READY for s in self._slot_states)
        )
        if not all_ready:
            return
        try:
            from saved_config import SavedConfig
            from saved_config_store import save_last_success
            save_last_success(SavedConfig(
                name="__last_success__",
                model_name="",
                device_type="",
                port=0,
                docker_image="",
                inference_engine="",
                options_json="{}",
                deploy_profile_name=profile.name,
                created="",
                last_used="",
            ))
        except Exception:
            pass  # never crash the orchestrator over a save failure
```

- [ ] **Step 4: Call it in `_run_sequential`**

In `app/profile_orchestrator.py`, find the end of `_run_sequential` (around line 345, just before `self._dispatch(self._cbs.on_profile_done)`). Add the call:

```python
        self._write_last_success_if_all_ready(profile)
        self._dispatch(self._cbs.on_profile_done)
```

(Replace the existing single `self._dispatch(self._cbs.on_profile_done)` line with those two lines.)

- [ ] **Step 5: Run to confirm pass**

```bash
PYTHONPATH=app pytest tests/test_profile_orchestrator.py::test_all_slots_ready_writes_last_success -v
```

Expected: `PASSED`

- [ ] **Step 6: Run full test suite**

```bash
PYTHONPATH=app pytest tests/ -v --tb=short 2>&1 | tail -20
```

Expected: All existing tests still pass.

- [ ] **Step 7: Commit**

```bash
git add app/profile_orchestrator.py tests/test_profile_orchestrator.py
git commit -m "feat: write last-success SavedConfig on ProfileOrchestrator all-READY"
```

---

## Task 5: DeployPanel.select_profile()

Add a public method so MainWindow can programmatically select a profile by name on startup restore.

**Files:**
- Modify: `app/deploy_panel.py`

No separate test file needed — the method is a thin wrapper over the existing `_refresh_profile_list` + list-box selection. Integration is verified in Task 6.

- [ ] **Step 1: Add `select_profile` to `DeployPanel`**

In `app/deploy_panel.py`, find the `_refresh_profile_list` method (around line 292). Just before it, add:

```python
    def select_profile(self, name: str) -> None:
        """Programmatically select the named profile in the sidebar list.

        Refreshes the list from disk first (so newly-saved profiles appear),
        then selects the row matching *name*. No-op if the profile no longer
        exists on disk.
        """
        self._refresh_profile_list()
        row = self._profile_list.get_first_child()
        while row is not None:
            if getattr(row, "_profile_name", None) == name:
                self._profile_list.select_row(row)
                return
            row = row.get_next_sibling()
```

- [ ] **Step 2: Verify no existing tests break**

```bash
PYTHONPATH=app pytest tests/ -v --tb=short 2>&1 | tail -10
```

Expected: All tests still pass.

- [ ] **Step 3: Commit**

```bash
git add app/deploy_panel.py
git commit -m "feat: add DeployPanel.select_profile(name) for startup restore"
```

---

## Task 6: MainWindow — restore on startup

Read last-success on startup and pre-fill the sidebar (model, device, port, docker image, options) or switch to the deploy panel.

**Files:**
- Modify: `app/main_window.py`

This task touches `MainWindow.__init__` and adds `_restore_config`. GTK widget interactions are difficult to unit-test without a display; verify manually by running the app.

- [ ] **Step 1: Add `_restore_config` method to `MainWindow`**

In `app/main_window.py`, find `MainWindow._on_state_changed` (around line 2928). Just before that method, add:

```python
    def _restore_config(self, cfg) -> None:
        """Pre-fill the UI from a SavedConfig (silent — no banners or toasts)."""
        if cfg.deploy_profile_name:
            # Profile launch: switch to deploy panel and select the profile.
            if self._deploy_panel is not None:
                deploy_toggle = getattr(self, "_deploy_toggle_btn", None)
                if deploy_toggle is not None:
                    deploy_toggle.set_active(True)
                self._main_stack.set_visible_child_name("deploy")
                self._deploy_panel.select_profile(cfg.deploy_profile_name)
        else:
            # Single-server launch: restore sidebar fields.
            if cfg.model_name:
                # select_model_by_id matches on model_name (the internal key).
                # We stored display_name; try a best-effort match by iterating
                # catalog entries to find model_name for this display_name.
                catalog = self._ctrl.catalog
                if catalog:
                    for entry in catalog.all_entries():
                        if entry.display_name == cfg.model_name:
                            self._sidebar.select_model_by_id(entry.model_name)
                            break
            if cfg.device_type:
                # _sidebar._device_dropdown uses a StringList; find the index.
                sl = self._sidebar._device_dropdown.get_model()
                if sl is not None:
                    for i in range(sl.get_n_items()):
                        if sl.get_string(i) == cfg.device_type:
                            self._sidebar._device_dropdown.set_selected(i)
                            break
            if cfg.port:
                self._sidebar._port_entry.set_text(str(cfg.port))
            if cfg.options_json and cfg.options_json != "{}":
                try:
                    import json, dataclasses
                    from launch_options import LaunchOptions
                    opts_dict = json.loads(cfg.options_json)
                    known = set(LaunchOptions.__dataclass_fields__)
                    opts = LaunchOptions(**{k: v for k, v in opts_dict.items() if k in known})
                    if self._panel._config_panel is not None:
                        self._panel._config_panel.load_options(opts)
                except Exception:
                    pass  # silently skip malformed options
```

- [ ] **Step 2: Call `_restore_config` at the end of `MainWindow.__init__`**

In `app/main_window.py`, find the end of `MainWindow.__init__` (around line 2924, after the keyboard shortcuts are registered). Add at the very end of `__init__`, before the closing of the method:

```python
        # Restore last successful launch config silently on startup.
        try:
            from saved_config_store import load_last_success
            _last = load_last_success()
            if _last:
                self._restore_config(_last)
        except Exception:
            pass
```

- [ ] **Step 3: Run the app and verify restore works**

```bash
cd /home/ttuser/code/tt-model-runner
./run
```

Expected: If a last-success config was previously saved, the sidebar fields are pre-filled. On first run (no `__last_success__.json`), the sidebar shows defaults.

- [ ] **Step 4: Run test suite**

```bash
PYTHONPATH=app pytest tests/ -v --tb=short 2>&1 | tail -10
```

Expected: All existing tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/main_window.py
git commit -m "feat: restore last-success config on MainWindow startup"
```

---

## Task 7: Saved Configs section in Sidebar

Add the SAVED CONFIGS dropdown + Load / Save / Delete controls above the Launch button in the sidebar.

**Files:**
- Modify: `app/main_window.py` (the `Sidebar` class, `_build` method and new methods)

**Context:** `Sidebar._build` is a long method starting around line 175. The Launch button is built around line 322–330 (`self._launch_btn`). Insert the Saved Configs section immediately before that block. The section is always shown; on first run the dropdown contains a placeholder entry and Load/Delete are insensitive.

- [ ] **Step 1: Add `_build_saved_configs_section` to `Sidebar`**

In `app/main_window.py`, find `Sidebar._build_settings_popover` (around line 370). Just before that method, add the new helper:

```python
    def _build_saved_configs_section(self) -> Gtk.Box:
        """Build the SAVED CONFIGS section: label + dropdown + Load/Save/Delete buttons."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_start(8); box.set_margin_end(8)
        box.set_margin_top(4);   box.set_margin_bottom(4)

        hdr = Gtk.Label(label="SAVED CONFIGS")
        hdr.add_css_class("section-label")
        hdr.set_halign(Gtk.Align.START)
        box.append(hdr)

        # Dropdown
        self._saved_cfg_list = Gtk.StringList()
        self._saved_cfg_dropdown = Gtk.DropDown(model=self._saved_cfg_list)
        self._saved_cfg_dropdown.set_hexpand(True)
        box.append(self._saved_cfg_dropdown)

        # Button row
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._saved_cfg_load_btn = Gtk.Button(label="Load")
        self._saved_cfg_load_btn.set_hexpand(True)
        self._saved_cfg_load_btn.connect("clicked", lambda _: self._on_saved_cfg_load())
        btn_row.append(self._saved_cfg_load_btn)

        self._saved_cfg_save_btn = Gtk.Button(label="Save")
        self._saved_cfg_save_btn.set_hexpand(True)
        self._saved_cfg_save_btn.connect("clicked", lambda _: self._on_saved_cfg_save())
        btn_row.append(self._saved_cfg_save_btn)

        self._saved_cfg_delete_btn = Gtk.Button(label="Delete")
        self._saved_cfg_delete_btn.set_hexpand(True)
        self._saved_cfg_delete_btn.connect("clicked", lambda _: self._on_saved_cfg_delete())
        btn_row.append(self._saved_cfg_delete_btn)

        box.append(btn_row)

        # Inline name entry (hidden by default, shown during Save flow)
        self._saved_cfg_name_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._saved_cfg_name_entry = Gtk.Entry()
        self._saved_cfg_name_entry.set_placeholder_text("Config name…")
        self._saved_cfg_name_entry.set_hexpand(True)
        self._saved_cfg_name_entry.connect("activate", lambda _: self._on_saved_cfg_confirm_save())
        confirm_btn = Gtk.Button(label="✓")
        confirm_btn.connect("clicked", lambda _: self._on_saved_cfg_confirm_save())
        self._saved_cfg_name_row.append(self._saved_cfg_name_entry)
        self._saved_cfg_name_row.append(confirm_btn)
        self._saved_cfg_name_row.set_visible(False)
        box.append(self._saved_cfg_name_row)

        self._refresh_saved_configs_section()
        return box

    def _refresh_saved_configs_section(self) -> None:
        """Rebuild the saved configs dropdown from disk."""
        from saved_config_store import list_named, load_last_success
        sl = self._saved_cfg_list
        # Clear existing items
        while sl.get_n_items() > 0:
            sl.remove(0)

        has_last = load_last_success() is not None
        named = list_named()

        if not has_last and not named:
            sl.append("(no saved configs yet)")
            self._saved_cfg_load_btn.set_sensitive(False)
            self._saved_cfg_delete_btn.set_sensitive(False)
            return

        self._saved_cfg_load_btn.set_sensitive(True)
        if has_last:
            sl.append("Last Success")
        for name in named:
            sl.append(name)
        self._saved_cfg_dropdown.set_selected(0)
        self._saved_cfg_delete_btn.set_sensitive(
            self._saved_cfg_dropdown.get_selected() != 0 or not has_last
        )

    def _get_selected_saved_config_name(self) -> str:
        """Return the raw name for the currently selected dropdown entry."""
        idx = self._saved_cfg_dropdown.get_selected()
        sl = self._saved_cfg_list
        if idx >= sl.get_n_items():
            return ""
        label = sl.get_string(idx)
        if label == "Last Success":
            return "__last_success__"
        return label

    def _on_saved_cfg_load(self) -> None:
        """Load the selected config; callback to MainWindow._restore_config set externally."""
        from saved_config_store import load_last_success, load_named
        name = self._get_selected_saved_config_name()
        if not name or name == "(no saved configs yet)":
            return
        try:
            if name == "__last_success__":
                cfg = load_last_success()
            else:
                cfg = load_named(name)
        except Exception:
            return
        if cfg and self.on_restore_config:
            self.on_restore_config(cfg)

    def _on_saved_cfg_save(self) -> None:
        """Toggle the inline name-entry row for saving a new named config."""
        self._saved_cfg_name_row.set_visible(True)
        self._saved_cfg_name_entry.grab_focus()

    def _on_saved_cfg_confirm_save(self) -> None:
        """Commit the named save: collect sidebar state, write to disk, refresh."""
        name = self._saved_cfg_name_entry.get_text().strip()
        if not name:
            return
        self._saved_cfg_name_row.set_visible(False)
        self._saved_cfg_name_entry.set_text("")
        if self.on_save_config:
            self.on_save_config(name)
        self._refresh_saved_configs_section()

    def _on_saved_cfg_delete(self) -> None:
        """Delete the selected named config from disk and refresh."""
        from saved_config_store import delete_named
        name = self._get_selected_saved_config_name()
        if not name or name == "__last_success__" or name == "(no saved configs yet)":
            return
        try:
            delete_named(name)
        except Exception:
            pass
        self._refresh_saved_configs_section()
```

- [ ] **Step 2: Add callback hooks to `Sidebar.__init__`**

In `app/main_window.py`, find `Sidebar.__init__` (around line 142). After the `self._on_pull = on_pull` assignments, add:

```python
        self.on_restore_config = None   # set by MainWindow; callable(SavedConfig)
        self.on_save_config = None      # set by MainWindow; callable(name: str)
```

- [ ] **Step 3: Call `_build_saved_configs_section` in `Sidebar._build`**

In `app/main_window.py`, find `Sidebar._build` where the Launch button row is constructed (around line 322, `btnbox = Gtk.Box(); ...`). Insert immediately before that block:

```python
        # Saved configs section
        saved_cfg_section = self._build_saved_configs_section()
        self.append(saved_cfg_section)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
```

- [ ] **Step 4: Wire `on_restore_config` and `on_save_config` in `MainWindow.__init__`**

In `app/main_window.py`, find `MainWindow.__init__` where controller callbacks are wired (around line 2865). After those lines, add:

```python
        # Wire saved-config sidebar callbacks.
        self._sidebar.on_restore_config = self._restore_config
        self._sidebar.on_save_config = self._on_save_config_from_sidebar
```

- [ ] **Step 5: Add `_on_save_config_from_sidebar` to `MainWindow`**

In `app/main_window.py`, add this method near `_restore_config`:

```python
    def _on_save_config_from_sidebar(self, name: str) -> None:
        """Called when user confirms a named save in the sidebar."""
        from saved_config import SavedConfig
        from saved_config_store import save_named
        import json, dataclasses
        entry = self._ctrl.current_entry
        opts = self._panel.get_options() or self._ctrl._options
        try:
            save_named(SavedConfig(
                name=name,
                model_name=entry.display_name if entry else "",
                device_type=entry.device_type if entry else "",
                port=int(self._sidebar.get_port()),
                docker_image=opts.docker_image_override if opts else "",
                inference_engine=entry.inference_engine if entry else "",
                options_json=json.dumps(dataclasses.asdict(opts)) if opts else "{}",
                deploy_profile_name="",
                created="",
                last_used="",
            ))
        except Exception:
            pass
        self._sidebar._refresh_saved_configs_section()
```

- [ ] **Step 6: Call `_refresh_saved_configs_section` from `_on_state_changed` on READY**

In `app/main_window.py`, find `MainWindow._on_state_changed` (around line 2928). Inside the `elif state == ServerState.READY:` branch (around line 2966), add at the end of that branch:

```python
            self._sidebar._refresh_saved_configs_section()
```

- [ ] **Step 7: Run the app and verify the section appears**

```bash
./run
```

Expected:
- On first run: "SAVED CONFIGS" section shows dropdown with "(no saved configs yet)", Load/Delete insensitive.
- After a successful launch (READY): section refreshes to show "Last Success".
- Load selects Last Success and pre-fills fields.
- Save flow: clicking Save shows inline entry; typing a name + Enter saves it and refreshes dropdown.
- Delete: removes named config (not "Last Success").

- [ ] **Step 8: Run test suite**

```bash
PYTHONPATH=app pytest tests/ -v --tb=short 2>&1 | tail -10
```

Expected: All tests pass.

- [ ] **Step 9: Commit**

```bash
git add app/main_window.py
git commit -m "feat: add Saved Configs section to sidebar with load/save/delete"
```

---

## Task 8: Copy env vars popover

Show a "⎘ env" button in the sidebar when READY; clicking it opens a popover with `VLLM_BASE_URL`, `VLLM_MODEL`, `api_key` values and copy buttons.

**Files:**
- Modify: `app/main_window.py` (Sidebar class)

**Context:** The env button lives in the sidebar below the launch/stop button. It appears only when `state == ServerState.READY`. `MainWindow._on_state_changed` controls visibility via a new `Sidebar.set_env_context` / `Sidebar.set_env_visible` API.

- [ ] **Step 1: Add env button to `Sidebar._build`**

In `app/main_window.py`, in `Sidebar._build`, find the block appending the launch button row (around line 329–330: `btnbox.append(self._launch_btn); self.append(btnbox)`). After `self.append(btnbox)`, add:

```python
        # Env vars button — shown only when state == READY
        env_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        env_row.set_margin_start(8); env_row.set_margin_end(8)
        env_row.set_margin_bottom(4)
        self._env_btn = Gtk.Button(label="⎘ env")
        self._env_btn.add_css_class("flat")
        self._env_btn.set_tooltip_text("Copy connection env vars for external tools")
        self._env_btn.set_hexpand(True)
        self._env_btn.connect("clicked", self._on_env_btn_clicked)
        self._env_btn.set_visible(False)
        env_row.append(self._env_btn)
        self.append(env_row)
        self._env_port: str = "8000"
        self._env_model_id: str = ""
```

- [ ] **Step 2: Add `set_env_context` and `set_env_visible` to `Sidebar`**

In `app/main_window.py`, add these two methods to `Sidebar` (near the other `set_*` public methods, around line 990–1002):

```python
    def set_env_context(self, port: str, model_id: str) -> None:
        """Store connection info for the env-vars popover."""
        self._env_port = port
        self._env_model_id = model_id

    def set_env_visible(self, visible: bool) -> None:
        """Show or hide the env-vars copy button."""
        self._env_btn.set_visible(visible)
```

- [ ] **Step 3: Add `_on_env_btn_clicked` and `_build_env_popover` to `Sidebar`**

In `app/main_window.py`, add these methods to `Sidebar` (near `_on_settings_clicked`, around line 414):

```python
    def _on_env_btn_clicked(self, btn) -> None:
        """Build and show the env-vars popover anchored to the env button."""
        popover = self._build_env_popover()
        popover.set_parent(btn)
        popover.popup()

    def _build_env_popover(self) -> "Gtk.Popover":
        """Build a Gtk.Popover with copyable VLLM_BASE_URL / VLLM_MODEL / api_key rows."""
        url = f"http://localhost:{self._env_port}/v1"
        model = self._env_model_id or "default"

        rows = [
            ("VLLM_BASE_URL", url),
            ("VLLM_MODEL",    model),
            ("api_key",       "none"),
        ]
        export_block = "\n".join(f"export {k}={v}" for k, v in rows)

        popover = Gtk.Popover()
        popover.set_autohide(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(12); box.set_margin_end(12)
        box.set_margin_top(10);   box.set_margin_bottom(10)
        box.set_size_request(340, -1)

        for key, val in rows:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            lbl = Gtk.Label(label=f"{key}={val}")
            lbl.set_hexpand(True)
            lbl.set_xalign(0)
            lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            lbl.add_css_class("muted")
            copy_btn = Gtk.Button(label="⎘")
            copy_btn.add_css_class("flat")
            copy_btn.set_tooltip_text(f"Copy {key} value")
            _val = val  # capture by value
            copy_btn.connect("clicked", lambda _, v=_val: (
                Gdk.Display.get_default().get_clipboard().set(v) if Gdk.Display.get_default() else None
            ))
            row.append(lbl)
            row.append(copy_btn)
            box.append(row)

        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        copy_all_btn = Gtk.Button(label="Copy all as export")
        copy_all_btn.set_hexpand(True)
        _block = export_block  # capture
        copy_all_btn.connect("clicked", lambda _, p=popover, b=_block: (
            (Gdk.Display.get_default().get_clipboard().set(b) if Gdk.Display.get_default() else None),
            p.popdown(),
        ))
        box.append(copy_all_btn)

        popover.set_child(box)
        return popover
```

- [ ] **Step 4: Wire visibility in `MainWindow._on_state_changed`**

In `app/main_window.py`, find `MainWindow._on_state_changed` (around line 2928). Inside the `elif state == ServerState.READY:` branch, add after the `_panel.set_curl_context(...)` line:

```python
            entry = self._ctrl.current_entry
            model_id = entry.hf_model_repo if entry else ""
            self._sidebar.set_env_context(port, model_id)
            self._sidebar.set_env_visible(True)
```

Also, at the top of `_on_state_changed`, add (so the button hides when state changes away from READY):

```python
        if state != ServerState.READY:
            self._sidebar.set_env_visible(False)
```

Place that `if` as the first statement in `_on_state_changed`, right after the method signature.

- [ ] **Step 5: Run the app and verify the env button**

```bash
./run
```

Expected:
- Before launch: no "⎘ env" button visible.
- After READY: "⎘ env" button appears below Launch/Stop button.
- Click "⎘ env": popover opens with three rows (VLLM_BASE_URL, VLLM_MODEL, api_key).
- Individual `⎘` copies just the value; "Copy all as export" copies the full export block and closes the popover.
- After Stop: "⎘ env" button disappears.

- [ ] **Step 6: Run test suite**

```bash
PYTHONPATH=app pytest tests/ -v --tb=short 2>&1 | tail -10
```

Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
git add app/main_window.py
git commit -m "feat: add env vars popover to sidebar (shown when READY)"
```

---

## Self-Review

### Spec coverage check

| Spec requirement | Task that covers it |
|-----------------|-------------------|
| Auto-restore last successful config on startup | Task 3 (write) + Task 6 (restore) |
| Named saved configs | Task 2 (store) + Task 7 (UI) |
| "Last Success" always first in dropdown | Task 7 `_refresh_saved_configs_section` |
| Restore is silent (no banner/toast) | Task 6 `_restore_config` — no emit, no log |
| Save trigger: single-server READY | Task 3 `_on_health_ready` hook |
| Save trigger: all profile slots READY | Task 4 `_write_last_success_if_all_ready` |
| Dropdown Load/Delete insensitive on first run | Task 7 `_refresh_saved_configs_section` |
| Section refreshes dropdown after READY | Task 7 Step 6 |
| DeployPanel.select_profile for profile restore | Task 5 |
| options_json forward-compat (unknown keys dropped) | Task 6 `_restore_config` — `if k in known` filter |
| Copy env vars: VLLM_BASE_URL, VLLM_MODEL, api_key | Task 8 |
| Individual line copy vs. "Copy all as export" | Task 8 `_build_env_popover` |
| Env button hidden unless READY | Task 8 Step 4 |
| Path traversal guard | Task 2 `_config_path` — `_NAME_RE` + resolve check |
| Atomic writes | Task 2 `_write` — tmp + rename |
| `_last_launch_config` stored so `_on_health_ready` can read it | Task 3 Steps 3–4 |

### Placeholder scan

No TBD, no "handle edge cases" phrases, no "similar to Task N" shortcuts. All code blocks are complete.

### Type consistency

- `SavedConfig` defined in Task 1; used identically in Tasks 3, 4, 6, 7.
- `save_last_success` / `load_last_success` / `save_named` / `load_named` / `list_named` / `delete_named` — defined in Task 2, called by same name in Tasks 3, 4, 7.
- `Sidebar.set_env_context(port: str, model_id: str)` / `set_env_visible(visible: bool)` — defined in Task 8 Step 2, called in Task 8 Step 4.
- `Sidebar.on_restore_config` / `on_save_config` — added in Task 7 Step 2, wired in Task 7 Step 4.
- `DeployPanel.select_profile(name: str)` — defined in Task 5, called in Task 6 `_restore_config`.
- `_refresh_saved_configs_section()` — defined in Task 7 Step 1, called in Tasks 7 Steps 5+6 and `_on_state_changed`.
