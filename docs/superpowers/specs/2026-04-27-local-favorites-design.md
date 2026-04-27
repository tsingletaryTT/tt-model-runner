# Local Favorites / Last-Successful-Deployment Design

**Date:** 2026-04-27
**Branch:** feature/local-favorites (to be created from feature/deploy-profiles or main after that merges)
**Scope:** Part 1 of 2. Part 2 (proxy aggregator sidecar) is a separate brainstorm/spec/plan cycle.

---

## Goal

When the app reopens, the last successful deployment config is pre-filled automatically.
Users can also save, name, and restore explicit configs. When a server is READY, the GUI
exposes connection env vars for external tools (tt-zork1, tt-agents, etc.).

---

## Out of Scope (Part 2)

- Proxy / aggregator sidecar that merges multiple running inference servers behind a single URL.
- Any modification to tt-inference-server itself.

---

## Data Model

New file: `app/saved_config.py`

```python
from __future__ import annotations
from dataclasses import dataclass, field

@dataclass
class SavedConfig:
    name: str                    # "__last_success__" for auto; user-chosen for named
    model_name: str              # display name from catalog (empty string for profile-only saves)
    device_type: str             # e.g. "n300" (empty string for profile-only saves)
    port: int                    # 0 for profile-only saves
    docker_image: str            # full tag, e.g. "ghcr.io/tenstorrent/..."
    inference_engine: str        # "vllm" | "tt-transformers"
    options_json: str            # json.dumps(dataclasses.asdict(options))
    deploy_profile_name: str     # "" for single-server; profile name for multi-slot
    created: str                 # ISO timestamp, set on first save
    last_used: str               # ISO timestamp, updated on every restore
```

Rules:
- For a **single-server launch**, `deploy_profile_name` is `""` and all other fields are populated.
- For a **profile launch**, `deploy_profile_name` is set to the profile name; `model_name`,
  `device_type`, `port` are `""` / `0` (the authoritative per-slot config lives in the profile
  file itself).
- `options_json` stores a `json.dumps(dataclasses.asdict(launch_options))` snapshot so
  `LaunchOptions` fields survive across versions (unknown keys are dropped on load with a
  logged warning, matching the existing deploy_profile deserialization pattern).

---

## Store

New file: `app/saved_config_store.py`

Storage directory: `~/.config/tt-runner-gui/saved_configs/`

Files:
- `__last_success__.json` — auto-written on every READY transition; always present after first
  successful launch.
- `<name>.json` — user-named configs. Path traversal guard: names must match `^[\w\-. ]+$`
  (same pattern as deploy_profile_store).

Public API:

```python
def save_last_success(config: SavedConfig) -> None: ...
def load_last_success() -> SavedConfig | None: ...        # None if file absent
def save_named(config: SavedConfig) -> None: ...          # config.name must be non-empty and != "__last_success__"
def load_named(name: str) -> SavedConfig: ...             # raises FileNotFoundError if absent
def list_named() -> list[str]: ...                        # sorted alpha; excludes "__last_success__"
def delete_named(name: str) -> None: ...                  # raises ValueError if name == "__last_success__"
```

Implementation notes:
- Use `dataclasses.asdict()` → mutate the dict for timestamps → `json.dumps` (never mutate
  the passed object, matching the pattern established in deploy_profile_store.py).
- `_SAVED_CONFIGS_DIR` is a module-level constant so tests can monkeypatch it.
- Write to a `.tmp` file then `rename()` for atomic writes.

---

## Write Triggers

### Single-server launch (AppController)

`LaunchConfig` (which holds `docker_image_override`) is a local in `_do_launch` and is not
available when `_on_health_ready` fires later. Fix: store the config on the instance.

In `_do_launch`, after `config = build_launch_config(...)`:

```python
self._last_launch_config = config   # NEW: so _on_health_ready can snapshot it
```

In `_on_health_ready`, after the `self._transition(ServerState.READY, ...)` call:

```python
if self._current_entry and self._last_launch_config:
    from saved_config import SavedConfig
    from saved_config_store import save_last_success
    import json, dataclasses
    cfg = SavedConfig(
        name="__last_success__",
        model_name=self._current_entry.display_name,
        device_type=self._current_entry.device_type,
        port=int(self._last_launch_config.port),
        docker_image=self._last_launch_config.docker_image_override,
        inference_engine=self._current_entry.inference_engine,
        options_json=json.dumps(dataclasses.asdict(self._options)),
        deploy_profile_name="",
        created="",
        last_used="",
    )
    save_last_success(cfg)
```

Not called on reconnect — `_on_health_ready` guards with `self._current_entry` which is
`None` on a reconnect-detected server (see existing `_try_identify_model_from_health`).

### Profile launch (ProfileOrchestrator)

At the end of `_run_sequential`, after all slots have reached READY without error:

```python
cfg = SavedConfig(
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
)
save_last_success(cfg)
```

---

## Restore on Startup

In `MainWindow.__init__`, after all widgets are built and before `win.present()`:

```python
from saved_config_store import load_last_success
cfg = load_last_success()
if cfg:
    self._restore_config(cfg)
```

`_restore_config(cfg: SavedConfig)` on `MainWindow`:
- If `cfg.deploy_profile_name`:
  - Activate the deploy toggle button (switch stack to deploy panel).
  - Call `self._deploy_panel.select_profile(cfg.deploy_profile_name)` if that profile still
    exists on disk; silently skip if it has been deleted.
- Else (single-server):
  - Pre-select `cfg.model_name` in the model list (match by display name; silently skip if
    catalog no longer contains it).
  - Set device dropdown to `cfg.device_type`.
  - Set port entry to `str(cfg.port)`.
  - Set docker image entry to `cfg.docker_image`.
  - Restore `LaunchOptions` from `cfg.options_json` into `ConfigPanel`.

Restore is silent — no banner, no toast, no log line. The user just sees their last config
pre-filled.

---

## GTK Surface

### Saved Configs section (sidebar action card)

Location: `app/main_window.py`, inside the action card, just above the Launch button.

Layout:

```
SAVED CONFIGS                                    [section-label]
┌─────────────────────────────┐ [Load] [Save] [Delete]
│ Last Success                ▼│
└─────────────────────────────┘
```

- `Gtk.DropDown` with `Gtk.StringList` model.
  - First entry: `"Last Success"` (always present if `__last_success__.json` exists; hidden
    entirely if absent — i.e. on first-ever run).
  - Remaining entries: sorted named configs from `list_named()`.
- **Load button**: calls `self._restore_config(self._load_selected_config())`. No
  confirmation. `_load_selected_config()` is a private `MainWindow` helper that calls
  `load_last_success()` when "Last Success" is selected, or `load_named(name)` otherwise.
- **Save button**: switches the button row to show a single-line `Gtk.Entry` with a
  placeholder "Config name…" and a "✓" confirm button. On confirm, saves current sidebar
  state as a named config and calls `_refresh_saved_configs_section()` to rebuild the
  dropdown.
- **Delete button**: removes the selected named config; insensitive when "Last Success" is
  selected. No confirmation for named configs (they are easy to recreate).

On first-ever run (no last-success file, no named configs), the section is shown but the
dropdown contains a single disabled placeholder entry `"(no saved configs yet)"` and Load /
Delete are insensitive. After the first successful launch the controller's `on_state`
callback fires — `MainWindow._on_state` calls `_refresh_saved_configs_section()` when
`state == READY`, which rebuilds the dropdown (now populated with "Last Success") and makes
the buttons sensitive.

### Copy env vars popover (action card, READY state only)

A flat "⎘ env" button appears in the action card alongside the Stop button, only when
`state == ServerState.READY`.

Clicking it opens a `Gtk.Popover` anchored to the button:

**Single-server READY:**

```
┌──────────────────────────────────────────────┐
│ VLLM_BASE_URL=http://localhost:8000/v1   [⎘] │
│ VLLM_MODEL=meta-llama/Llama-3.1-8B      [⎘] │
│ api_key=none                             [⎘] │
│                        [Copy all as export]  │
└──────────────────────────────────────────────┘
```

**Profile READY (multi-slot):** one section per READY slot, labelled by slot model name.

"Copy all as export" copies:
```
export VLLM_BASE_URL=http://localhost:8000/v1
export VLLM_MODEL=meta-llama/Llama-3.1-8B-Instruct
export api_key=none
```
Popover closes after "Copy all." Individual `[⎘]` buttons copy just that line's value (not
the `export KEY=` prefix — just the value string, matching what you'd paste into a `.env`
file or a Python `os.environ` assignment).

Implementation: `Gtk.Popover` attached to the env button in the action card. The popover
is rebuilt each time it is opened (values come from live controller state, not stale
construction-time values).

---

## New Files

| File | Purpose |
|------|---------|
| `app/saved_config.py` | `SavedConfig` dataclass |
| `app/saved_config_store.py` | Store CRUD (`save_last_success`, `load_last_success`, `save_named`, etc.) |
| `tests/test_saved_config_store.py` | Unit tests for store (monkeypatched tmp dir) |

## Modified Files

| File | Change |
|------|--------|
| `app/controller.py` | Write last success on READY state transition |
| `app/profile_orchestrator.py` | Write last success when all slots READY |
| `app/main_window.py` | Restore on startup; Saved Configs section; env vars popover; `_refresh_saved_configs_section()` called from `_on_state` on READY |
| `app/deploy_panel.py` | Add `select_profile(name: str) -> None` — selects named profile in the left sidebar list if it exists, no-op if not found |

---

## Testing

- `test_saved_config_store.py`: round-trip save/load for last success and named configs;
  path traversal guard; `list_named` excludes `__last_success__`; `delete_named` raises on
  sentinel name; atomic write (tmp + rename).
- `test_controller.py`: on READY transition, `save_last_success` is called with correct
  fields. Use monkeypatched store.
- `test_profile_orchestrator.py`: on all-slots-READY, `save_last_success` called with
  `deploy_profile_name` set. Use monkeypatched store.
- GTK restore path: tested via existing `MainWindow` integration setup if feasible; otherwise
  verify `_restore_config` logic with a mock sidebar.

---

## Explicitly Not Changing

- The single-server launch flow in AppController (other than the READY hook above).
- The deploy panel or profile orchestrator logic (other than the READY hook above).
- `app_settings.py` — `last_deploy_profile` key already added but currently unused; leave it
  alone (this design supersedes it; we can remove that key in a cleanup pass later).
- The TUI view — TUI gets no Saved Configs UI in this iteration.
