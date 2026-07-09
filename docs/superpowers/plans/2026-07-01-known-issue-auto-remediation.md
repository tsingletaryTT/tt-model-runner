# Known-issue Auto-remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let tt-model-runner recognize known device/model incompatibilities (seeded with Adam Housman's P100 + Llama-3.1-8B L1-prefill crash), auto-apply the documented workaround, and relaunch — plus a headless `doctor` dry-run that reports what it would do.

**Architecture:** A bundled data-driven knowledge base (`data/workarounds.json`) is consumed by a pure, UI-free resolver (`app/workaround_resolver.py`). `AppController` consults it at two existing seams: pre-flight (before `_do_launch`) to pre-apply fixes, and the ERROR log scan to reactively apply-and-relaunch once. A tiny `--doctor` entry reuses the same resolver to print matches without mutating anything.

**Tech Stack:** Python 3 stdlib only (`json`, `re`, `fnmatch`, `dataclasses`, `pathlib`). Tests: `pytest`. GTK4 (PyGObject) + Textual for the two views.

## Global Constraints

- **No new third-party dependencies.** Stdlib only (`json`, `re`, `fnmatch`, `dataclasses`, `pathlib`, `subprocess`).
- **Resolver is pure / UI-free.** `app/workaround_resolver.py` must not import GTK, Textual, or `controller`. Same discipline as `device_detector.py` / `launch_options.py`.
- **Threading discipline.** Never touch widgets from background threads. All view notifications go through `AppController._emit(...)`; controller timers use `threading.Timer`.
- **Terminal output style.** Log banners use the house left-bar box (`╔`, `║`, `╚`) with **no right-side border** (per `~/CLAUDE.md`).
- **Reactive relaunch is capped at exactly 1 attempt** (`self._remediation_attempts`), guaranteeing no relaunch loops.
- **`.env` mutations auto-apply** but are always logged; `apply_remedy` records what it wrote so undo can scrub it.
- **Run tests with:** `cd /Users/tsingletary/code/tt-model-runner && PYTHONPATH=app pytest tests/ -v`
- **Version-gating fails open.** Unknown/undeterminable repo version → remedy is treated as applicable (the reactive path still catches real crashes).

---

## File Structure

- **Create** `data/workarounds.json` — the knowledge base; seeded with the P100 case.
- **Create** `app/workaround_resolver.py` — `Workaround` dataclass, `load_workarounds()`, `match_preflight()`, `match_symptom()`, `_glob_match()`, `_version_applies()`.
- **Create** `app/doctor_main.py` — headless `--doctor` dry-run CLI.
- **Create** `tests/test_workaround_resolver.py` — resolver unit tests.
- **Create** `tests/test_doctor.py` — doctor CLI tests.
- **Modify** `app/server_manager.py` — add `_set_env_key(env_path, key, value)` next to `_scrub_env_key`.
- **Modify** `app/controller.py` — `AppliedRemedy` dataclass, `_apply_remedy()`, `undo_remediation()`, pre-flight hook, reactive retry hook, new instance vars, `on_remediation_applied` emission.
- **Modify** `tests/test_controller_contract.py` — add `on_remediation_applied` to `ViewContract`, `GtkViewStub`, `TuiViewStub`.
- **Modify** `tests/test_controller.py` — pre-flight / reactive / undo controller tests.
- **Modify** `tests/test_server_manager.py` (or nearest existing) — `_set_env_key` round-trip test.
- **Modify** `run` — route `--doctor` to `app/doctor_main.py` (mirrors `--tui`).
- **Modify** `app/main_window.py` (GTK) and `app/tui/app.py` (TUI) — implement `on_remediation_applied`.

---

## Task 1: Knowledge base file + `Workaround` dataclass + loader

**Files:**
- Create: `data/workarounds.json`
- Create: `app/workaround_resolver.py`
- Test: `tests/test_workaround_resolver.py`

**Interfaces:**
- Produces: `Workaround` dataclass with fields `id: str`, `devices: list[str]`, `models: list[str]`, `symptom: Optional[str]`, `env: dict[str, str]`, `vllm: dict`, `also_move_to_env: list[str]`, `auto: bool`, `tradeoff: str`, `ref: str`, `applies_to_versions: Optional[str]`.
- Produces: `load_workarounds(path: Optional[Path] = None) -> list[Workaround]` — reads the bundled JSON (default: `<repo>/data/workarounds.json`), tolerates missing/optional fields.

- [ ] **Step 1: Write the seed KB file**

Create `data/workarounds.json`:

```json
[
  {
    "id": "p100-llama31-8b-l1-prefill",
    "devices": ["P100", "P150", "P300"],
    "models": ["Llama-3.1-8B*"],
    "symptom": "clash with L1 buffers",
    "env": { "MAX_PREFILL_CHUNK_SIZE": "2" },
    "vllm": { "max_model_len": 1024 },
    "also_move_to_env": ["HF_TOKEN", "JWT_SECRET"],
    "auto": true,
    "tradeoff": "Requests needing a >1024-token prefill will fail — a hard P100 limit (MIN_CHUNK_SIZE=2048).",
    "ref": "tt-metal#28835",
    "applies_to_versions": "<=0.10.0"
  }
]
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_workaround_resolver.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the workaround resolver (pure logic, no UI)."""
from pathlib import Path

import workaround_resolver as wr


def test_load_seed_kb_has_p100_entry():
    items = wr.load_workarounds()
    ids = {w.id for w in items}
    assert "p100-llama31-8b-l1-prefill" in ids

    w = next(w for w in items if w.id == "p100-llama31-8b-l1-prefill")
    assert "P100" in w.devices
    assert w.models == ["Llama-3.1-8B*"]
    assert w.symptom == "clash with L1 buffers"
    assert w.env == {"MAX_PREFILL_CHUNK_SIZE": "2"}
    assert w.vllm == {"max_model_len": 1024}
    assert w.also_move_to_env == ["HF_TOKEN", "JWT_SECRET"]
    assert w.auto is True
    assert w.ref == "tt-metal#28835"
    assert w.applies_to_versions == "<=0.10.0"


def test_load_tolerates_optional_fields(tmp_path: Path):
    kb = tmp_path / "wa.json"
    kb.write_text('[{"id": "minimal", "symptom": "boom"}]')
    items = wr.load_workarounds(kb)
    assert len(items) == 1
    w = items[0]
    assert w.id == "minimal"
    assert w.devices == []          # defaulted
    assert w.models == []           # defaulted
    assert w.env == {}              # defaulted
    assert w.auto is True           # defaults to auto-applicable
    assert w.applies_to_versions is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd /Users/tsingletary/code/tt-model-runner && PYTHONPATH=app pytest tests/test_workaround_resolver.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'workaround_resolver'`

- [ ] **Step 4: Write minimal implementation**

Create `app/workaround_resolver.py`:

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Data-driven known-issue workarounds.

Pure logic — NO GTK / Textual / controller imports.  Matches a device+model
(and optionally a crash log line) against a bundled knowledge base and returns
the workarounds that apply.  See docs/superpowers/specs/2026-07-01-*.md.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

_DEFAULT_KB = Path(__file__).resolve().parent.parent / "data" / "workarounds.json"


@dataclass
class Workaround:
    id: str
    devices: List[str] = field(default_factory=list)
    models: List[str] = field(default_factory=list)
    symptom: Optional[str] = None
    env: dict = field(default_factory=dict)
    vllm: dict = field(default_factory=dict)
    also_move_to_env: List[str] = field(default_factory=list)
    auto: bool = True
    tradeoff: str = ""
    ref: str = ""
    applies_to_versions: Optional[str] = None


def load_workarounds(path: Optional[Path] = None) -> List[Workaround]:
    """Load and parse the knowledge base. Returns [] on any read/parse error."""
    p = Path(path) if path else _DEFAULT_KB
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    out: List[Workaround] = []
    for obj in raw:
        if not isinstance(obj, dict) or "id" not in obj:
            continue
        out.append(Workaround(
            id=obj["id"],
            devices=list(obj.get("devices", [])),
            models=list(obj.get("models", [])),
            symptom=obj.get("symptom"),
            env=dict(obj.get("env", {})),
            vllm=dict(obj.get("vllm", {})),
            also_move_to_env=list(obj.get("also_move_to_env", [])),
            auto=bool(obj.get("auto", True)),
            tradeoff=obj.get("tradeoff", ""),
            ref=obj.get("ref", ""),
            applies_to_versions=obj.get("applies_to_versions"),
        ))
    return out
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /Users/tsingletary/code/tt-model-runner && PYTHONPATH=app pytest tests/test_workaround_resolver.py -v`
Expected: PASS (both tests)

- [ ] **Step 6: Commit**

```bash
git add data/workarounds.json app/workaround_resolver.py tests/test_workaround_resolver.py
git commit -m "feat: workaround KB file + Workaround dataclass + loader"
```

---

## Task 2: Matching logic (`match_preflight`, `match_symptom`, version gating)

**Files:**
- Modify: `app/workaround_resolver.py`
- Test: `tests/test_workaround_resolver.py`

**Interfaces:**
- Consumes: `Workaround`, `load_workarounds` from Task 1.
- Produces:
  - `_glob_match(pattern: str, value: str) -> bool` — case-insensitive `fnmatch`.
  - `_version_applies(constraint: Optional[str], version: Optional[str]) -> bool` — supports `<=`, `>=`, `==`, `<`, `>` prefixes on a dotted version; `None` constraint or `None` version → `True` (fail open).
  - `match_preflight(device: str, model: str, repo_version: Optional[str] = None, kb: Optional[list] = None) -> list[Workaround]` — device+model (+version) match, ignoring symptom.
  - `match_symptom(log_line: str, device: str, model: str, kb: Optional[list] = None) -> Optional[Workaround]` — first entry whose device+model match AND whose `symptom` regex hits the line.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workaround_resolver.py`:

```python
import re


def _kb():
    return [
        wr.Workaround(
            id="p100", devices=["P100", "P150"], models=["Llama-3.1-8B*"],
            symptom="clash with L1 buffers", env={"MAX_PREFILL_CHUNK_SIZE": "2"},
            vllm={"max_model_len": 1024}, applies_to_versions="<=0.10.0",
        ),
        wr.Workaround(id="anydev", devices=[], models=[], symptom="generic boom"),
    ]


def test_glob_match_case_insensitive():
    assert wr._glob_match("Llama-3.1-8B*", "llama-3.1-8b-instruct")
    assert not wr._glob_match("Llama-3.1-8B*", "Qwen3-8B")


def test_version_applies():
    assert wr._version_applies("<=0.10.0", "0.10.0")
    assert wr._version_applies("<=0.10.0", "0.9.3")
    assert not wr._version_applies("<=0.10.0", "0.11.0")
    assert wr._version_applies(None, "0.11.0")      # no constraint
    assert wr._version_applies("<=0.10.0", None)    # unknown version → fail open


def test_match_preflight_device_and_model():
    hits = wr.match_preflight("P100", "Llama-3.1-8B-Instruct", "0.10.0", kb=_kb())
    assert [w.id for w in hits] == ["p100", "anydev"]  # anydev matches everything


def test_match_preflight_wrong_device():
    hits = wr.match_preflight("N150", "Llama-3.1-8B-Instruct", "0.10.0", kb=_kb())
    assert [w.id for w in hits] == ["anydev"]


def test_match_preflight_version_skips():
    hits = wr.match_preflight("P100", "Llama-3.1-8B-Instruct", "0.11.0", kb=_kb())
    assert [w.id for w in hits] == ["anydev"]  # p100 gated out by version


def test_match_symptom_hits_regex():
    line = "TT_THROW: Statically allocated circular buffers ... clash with L1 buffers"
    w = wr.match_symptom(line, "P100", "Llama-3.1-8B-Instruct", kb=_kb())
    assert w is not None and w.id == "p100"


def test_match_symptom_no_match_on_wrong_model():
    line = "clash with L1 buffers"
    w = wr.match_symptom(line, "P100", "Qwen3-8B", kb=_kb())
    assert w is not None and w.id == "anydev"  # falls through to the any/any generic


def test_match_symptom_none_when_regex_misses():
    w = wr.match_symptom("all good here", "P100", "Llama-3.1-8B-Instruct", kb=_kb())
    assert w is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/tsingletary/code/tt-model-runner && PYTHONPATH=app pytest tests/test_workaround_resolver.py -v`
Expected: FAIL with `AttributeError: module 'workaround_resolver' has no attribute '_glob_match'`

- [ ] **Step 3: Write minimal implementation**

Append to `app/workaround_resolver.py`:

```python
import fnmatch
import re


def _glob_match(pattern: str, value: str) -> bool:
    return fnmatch.fnmatch(value.lower(), pattern.lower())


def _version_applies(constraint: Optional[str], version: Optional[str]) -> bool:
    """True if `version` satisfies `constraint` (e.g. '<=0.10.0'). Fail open."""
    if not constraint or not version:
        return True
    ops = [("<=", lambda a, b: a <= b), (">=", lambda a, b: a >= b),
           ("==", lambda a, b: a == b), ("<", lambda a, b: a < b),
           (">", lambda a, b: a > b)]
    for prefix, cmp in ops:
        if constraint.startswith(prefix):
            target = constraint[len(prefix):].strip()
            try:
                va = tuple(int(x) for x in version.lstrip("v").split("."))
                vb = tuple(int(x) for x in target.lstrip("v").split("."))
            except ValueError:
                return True  # unparseable → fail open
            # pad to equal length so (0, 10) vs (0, 10, 0) compares correctly
            n = max(len(va), len(vb))
            va += (0,) * (n - len(va))
            vb += (0,) * (n - len(vb))
            return cmp(va, vb)
    return True  # unrecognized constraint syntax → fail open


def _device_matches(w: "Workaround", device: str) -> bool:
    return not w.devices or device.upper() in {d.upper() for d in w.devices}


def _model_matches(w: "Workaround", model: str) -> bool:
    return not w.models or any(_glob_match(p, model) for p in w.models)


def match_preflight(device: str, model: str,
                    repo_version: Optional[str] = None,
                    kb: Optional[List["Workaround"]] = None) -> List["Workaround"]:
    items = kb if kb is not None else load_workarounds()
    return [w for w in items
            if _device_matches(w, device)
            and _model_matches(w, model)
            and _version_applies(w.applies_to_versions, repo_version)]


def match_symptom(log_line: str, device: str, model: str,
                  kb: Optional[List["Workaround"]] = None) -> Optional["Workaround"]:
    items = kb if kb is not None else load_workarounds()
    for w in items:
        if not w.symptom:
            continue
        if not (_device_matches(w, device) and _model_matches(w, model)):
            continue
        if re.search(w.symptom, log_line, re.I):
            return w
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/tsingletary/code/tt-model-runner && PYTHONPATH=app pytest tests/test_workaround_resolver.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add app/workaround_resolver.py tests/test_workaround_resolver.py
git commit -m "feat: workaround matching (device/model glob, version gate, symptom regex)"
```

---

## Task 3: `_set_env_key` helper in `server_manager`

**Files:**
- Modify: `app/server_manager.py` (add after `_scrub_env_key`, ~line 255)
- Test: `tests/test_server_manager_env.py` (create)

**Interfaces:**
- Consumes: existing `_scrub_env_key(env_path: Path, key: str)`.
- Produces: `_set_env_key(env_path: Path, key: str, value: str) -> None` — idempotent set-or-replace of a single `KEY=value` line; creates the file if absent; preserves other lines.

- [ ] **Step 1: Write the failing test**

Create `tests/test_server_manager_env.py`:

```python
# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

from server_manager import _set_env_key, _scrub_env_key


def test_set_creates_file(tmp_path: Path):
    env = tmp_path / ".env"
    _set_env_key(env, "MAX_PREFILL_CHUNK_SIZE", "2")
    assert env.read_text().strip() == "MAX_PREFILL_CHUNK_SIZE=2"


def test_set_replaces_existing_and_preserves_others(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("HF_TOKEN=abc\nMAX_PREFILL_CHUNK_SIZE=8\nJWT_SECRET=xyz\n")
    _set_env_key(env, "MAX_PREFILL_CHUNK_SIZE", "2")
    text = env.read_text()
    assert "MAX_PREFILL_CHUNK_SIZE=2" in text
    assert "MAX_PREFILL_CHUNK_SIZE=8" not in text
    assert "HF_TOKEN=abc" in text
    assert "JWT_SECRET=xyz" in text


def test_set_then_scrub_round_trip(tmp_path: Path):
    env = tmp_path / ".env"
    _set_env_key(env, "MAX_PREFILL_CHUNK_SIZE", "2")
    _scrub_env_key(env, "MAX_PREFILL_CHUNK_SIZE")
    assert "MAX_PREFILL_CHUNK_SIZE" not in env.read_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/tsingletary/code/tt-model-runner && PYTHONPATH=app pytest tests/test_server_manager_env.py -v`
Expected: FAIL with `ImportError: cannot import name '_set_env_key'`

- [ ] **Step 3: Write minimal implementation**

Add to `app/server_manager.py` immediately after `_scrub_env_key`:

```python
def _set_env_key(env_path: Path, key: str, value: str) -> None:
    """Idempotently set KEY=value in a .env file.

    Replaces any existing line for `key`, preserves all other lines, and
    creates the file (and parent dir) if it does not exist.  Sibling of
    _scrub_env_key — together they support workaround apply/undo.
    """
    try:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        lines = (env_path.read_text().splitlines()
                 if env_path.exists() else [])
        kept = [l for l in lines if not l.strip().startswith(f"{key}=")]
        kept.append(f"{key}={value}")
        env_path.write_text("\n".join(kept) + "\n")
    except OSError:
        pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/tsingletary/code/tt-model-runner && PYTHONPATH=app pytest tests/test_server_manager_env.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add app/server_manager.py tests/test_server_manager_env.py
git commit -m "feat: _set_env_key helper for workaround .env writes"
```

---

## Task 4: `AppliedRemedy` + `_apply_remedy` + `undo_remediation` on controller

**Files:**
- Modify: `app/controller.py` (imports near top; new instance vars in `__init__` ~line 275; new dataclass near other dataclasses; new methods near `restart`)
- Test: `tests/test_controller.py`

**Interfaces:**
- Consumes: `Workaround` (Task 1/2), `_set_env_key`/`_scrub_env_key` (Task 3), existing `LaunchOptions`, `self._options`, `self._emit`.
- Produces:
  - `AppliedRemedy` dataclass: `remedy: Workaround`, `env_keys_written: list[str]`.
  - `AppController._apply_remedy(self, w: Workaround, repo_path: Path) -> AppliedRemedy` — merges `w.vllm` into `self._options`, writes `w.env` + relocates `w.also_move_to_env` into repo-root `.env`, stores `self._applied_remedy`, emits the banner + `on_remediation_applied`.
  - `AppController.undo_remediation(self) -> None` — scrubs the written `.env` keys, clears the vllm overrides it set, clears `self._applied_remedy`.
- New instance vars: `self._applied_remedy: Optional[AppliedRemedy] = None`, `self._remediation_attempts: int = 0`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_controller.py` (reuse the file's existing controller-construction helper; the sketch below assumes a `make_controller()` returning a controller with `NullDispatch`. If the existing tests build the controller differently, match that pattern):

```python
from pathlib import Path
import workaround_resolver as wr


def test_apply_remedy_writes_env_and_sets_override(tmp_path, monkeypatch):
    ctrl = make_controller()                      # existing helper w/ sync dispatch
    monkeypatch.setattr("controller._settings.server_repo_path", str(tmp_path))
    w = wr.Workaround(
        id="p100", devices=["P100"], models=["Llama-3.1-8B*"],
        symptom="clash with L1 buffers", env={"MAX_PREFILL_CHUNK_SIZE": "2"},
        vllm={"max_model_len": 1024}, also_move_to_env=["HF_TOKEN"],
        tradeoff="context capped", ref="tt-metal#28835",
    )
    monkeypatch.setenv("HF_TOKEN", "hf_secret")

    applied = ctrl._apply_remedy(w, Path(tmp_path))

    env_text = (tmp_path / ".env").read_text()
    assert "MAX_PREFILL_CHUNK_SIZE=2" in env_text
    assert "HF_TOKEN=hf_secret" in env_text          # relocated from environment
    assert ctrl._options.max_model_len == 1024
    assert ctrl._applied_remedy is applied
    assert "MAX_PREFILL_CHUNK_SIZE" in applied.env_keys_written


def test_undo_remediation_scrubs_and_clears(tmp_path, monkeypatch):
    ctrl = make_controller()
    monkeypatch.setattr("controller._settings.server_repo_path", str(tmp_path))
    w = wr.Workaround(id="p100", devices=["P100"], models=["*"],
                      env={"MAX_PREFILL_CHUNK_SIZE": "2"}, vllm={"max_model_len": 1024})
    ctrl._apply_remedy(w, Path(tmp_path))

    ctrl.undo_remediation()

    assert "MAX_PREFILL_CHUNK_SIZE" not in (tmp_path / ".env").read_text()
    assert ctrl._options.max_model_len is None
    assert ctrl._applied_remedy is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/tsingletary/code/tt-model-runner && PYTHONPATH=app pytest tests/test_controller.py -k remedy -v`
Expected: FAIL with `AttributeError: 'AppController' object has no attribute '_apply_remedy'`

- [ ] **Step 3: Write minimal implementation**

At the top of `app/controller.py`, add the import (near other app imports):

```python
import workaround_resolver as _wr
from server_manager import _set_env_key, _scrub_env_key
```

Near the other dataclasses (e.g. next to `BenchResult`), add:

```python
@dataclass
class AppliedRemedy:
    """Record of a workaround the controller applied, for logging + undo."""
    remedy: "_wr.Workaround"
    env_keys_written: list          # .env keys we wrote (to scrub on undo)
```

In `AppController.__init__`, after `self._emitted_error_hints = set()`:

```python
        self._applied_remedy: Optional[AppliedRemedy] = None
        self._remediation_attempts: int = 0   # reactive relaunches used (cap = 1)
```

Add these methods (near `restart`):

```python
    def _apply_remedy(self, w: "_wr.Workaround", repo_path: Path) -> AppliedRemedy:
        """Apply a known-issue workaround: merge vLLM overrides, write .env.

        Records what changed on self._applied_remedy so undo_remediation can
        reverse it.  Emits a transparent banner and on_remediation_applied.
        """
        env_path = repo_path / ".env"
        written: list = []

        # 1. vLLM overrides (reuses launch_options' max_model_len handling).
        if "max_model_len" in w.vllm:
            self._options.max_model_len = w.vllm["max_model_len"]

        # 2. env keys written verbatim into repo-root .env.
        for key, value in w.env.items():
            _set_env_key(env_path, key, str(value))
            written.append(key)

        # 3. relocate env vars that v0.10.0 only reads from .env.
        for name in w.also_move_to_env:
            val = os.environ.get(name, "")
            if val and not _env_file_has_key(env_path, name):
                _set_env_key(env_path, name, val)
                written.append(name)

        applied = AppliedRemedy(remedy=w, env_keys_written=written)
        self._applied_remedy = applied
        self._emit_remediation_banner(w)
        self._emit("on_remediation_applied", w)
        return applied

    def undo_remediation(self) -> None:
        """Reverse the last applied remedy: scrub .env keys, clear overrides."""
        applied = self._applied_remedy
        if not applied:
            return
        env_path = Path(_settings.server_repo_path) / ".env"
        for key in applied.env_keys_written:
            _scrub_env_key(env_path, key)
        if "max_model_len" in applied.remedy.vllm:
            self._options.max_model_len = None
        self._applied_remedy = None
        self._emit("on_log_line", f"↩ Reverted workaround {applied.remedy.id}")

    def _emit_remediation_banner(self, w: "_wr.Workaround", preflight: bool = True) -> None:
        """Log the house-style left-bar banner describing an applied remedy."""
        head = "PRE-FLIGHT" if preflight else "AUTO-RETRY"
        changes = []
        if w.env:
            changes.append(", ".join(f"{k}={v}" for k, v in w.env.items()))
        if "max_model_len" in w.vllm:
            changes.append(f"max_model_len={w.vllm['max_model_len']}")
        self._emit("on_log_line", "╔══════════════════════════════════════════")
        self._emit("on_log_line", f"║ {head}: known issue {w.ref or w.id}")
        if changes:
            self._emit("on_log_line", f"║ Applying: {'; '.join(changes)}")
        if w.tradeoff:
            self._emit("on_log_line", f"║ ⚠ {w.tradeoff}")
        self._emit("on_log_line", "║ [config logged · undo available]")
        self._emit("on_log_line", "╚══════════════════════════════════════════")
```

Also add the tiny helper near module-level helpers (used above):

```python
def _env_file_has_key(env_path: Path, key: str) -> bool:
    if not env_path.exists():
        return False
    try:
        return any(l.strip().startswith(f"{key}=")
                   for l in env_path.read_text().splitlines())
    except OSError:
        return False
```

> Note: `on_remediation_applied` is emitted here but the `ViewContract`/stub
> additions land in Task 5; the `NullDispatch` test view ignores unknown
> callbacks, so these tests pass before Task 5.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/tsingletary/code/tt-model-runner && PYTHONPATH=app pytest tests/test_controller.py -k remedy -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add app/controller.py tests/test_controller.py
git commit -m "feat: apply/undo known-issue remedy on controller (.env + vLLM overrides)"
```

---

## Task 5: Pre-flight hook + `on_remediation_applied` view contract

**Files:**
- Modify: `app/controller.py` (`_preflight_and_launch`, ~line 457-488)
- Modify: `tests/test_controller_contract.py` (`ViewContract`, `GtkViewStub`, `TuiViewStub`)
- Test: `tests/test_controller.py`

**Interfaces:**
- Consumes: `match_preflight` (Task 2), `_apply_remedy` (Task 4).
- Produces: pre-flight application of matching remedies before `_do_launch`. Adds `on_remediation_applied(self, remedy)` to the view contract.

- [ ] **Step 1: Add `on_remediation_applied` to the contract + stubs (failing contract test)**

In `tests/test_controller_contract.py`, add to `ViewContract` (after `on_environment_checked`):

```python
    @abstractmethod
    def on_remediation_applied(self, remedy): ...
```

Do **not** yet add the method to `GtkViewStub`/`TuiViewStub` — that makes the existing contract test fail (proving the contract is enforced).

- [ ] **Step 2: Run contract test to verify it fails**

Run: `cd /Users/tsingletary/code/tt-model-runner && PYTHONPATH=app pytest tests/test_controller_contract.py -v`
Expected: FAIL — `TypeError: Can't instantiate abstract class GtkViewStub with abstract method on_remediation_applied`

- [ ] **Step 3: Implement the stub methods**

In `tests/test_controller_contract.py`, add to both `GtkViewStub` and `TuiViewStub`:

```python
    def on_remediation_applied(self, remedy): pass
```

- [ ] **Step 4: Run contract test to verify it passes**

Run: `cd /Users/tsingletary/code/tt-model-runner && PYTHONPATH=app pytest tests/test_controller_contract.py -v`
Expected: PASS

- [ ] **Step 5: Write the failing pre-flight test**

Add to `tests/test_controller.py`:

```python
def test_preflight_applies_remedy_before_launch(tmp_path, monkeypatch):
    ctrl = make_controller()
    monkeypatch.setattr("controller._settings.server_repo_path", str(tmp_path))
    (tmp_path / "run.py").write_text("# stub")

    captured = {}
    monkeypatch.setattr(ctrl, "_do_launch",
                        lambda entry, port: captured.update(mml=ctrl._options.max_model_len))
    # Force the KB match regardless of bundled file contents.
    w = wr.Workaround(id="p100", devices=["P100"], models=["*"],
                      env={"MAX_PREFILL_CHUNK_SIZE": "2"}, vllm={"max_model_len": 1024})
    monkeypatch.setattr("controller._wr.match_preflight",
                        lambda device, model, repo_version=None: [w])

    entry = make_entry(device_type="P100", display_name="Llama-3.1-8B-Instruct")
    ctrl._preflight_apply(entry)               # new synchronous helper (see Step 7)
    ctrl._do_launch(entry, "8000")

    assert captured["mml"] == 1024
    assert "MAX_PREFILL_CHUNK_SIZE=2" in (tmp_path / ".env").read_text()
```

> `make_entry(...)` is a helper that builds a `ModelEntry`; if the test file
> lacks one, construct a `ModelEntry` inline with `device_type`,
> `display_name`, and `inference_engine="vllm"`.

- [ ] **Step 6: Run test to verify it fails**

Run: `cd /Users/tsingletary/code/tt-model-runner && PYTHONPATH=app pytest tests/test_controller.py -k preflight_applies -v`
Expected: FAIL with `AttributeError: 'AppController' object has no attribute '_preflight_apply'`

- [ ] **Step 7: Implement the pre-flight hook**

In `app/controller.py`, add a small synchronous helper and call it inside `_preflight_and_launch` just before `self._do_launch(entry, port)` (line ~488):

```python
    def _preflight_apply(self, entry: "ModelEntry") -> None:
        """Pre-apply any known-issue workarounds matching this device+model."""
        repo_path = Path(_settings.server_repo_path)
        try:
            hits = _wr.match_preflight(entry.device_type, entry.display_name,
                                       self._server_repo_version())
        except Exception:                        # resolver must never block launch
            hits = []
        for w in hits:
            if w.auto:
                self._apply_remedy(w, repo_path)
            else:
                self._emit("on_log_line",
                           f"⚠ Known issue {w.ref or w.id} may apply "
                           f"({w.tradeoff}) — not auto-applied (auto=false)")

    def _server_repo_version(self) -> Optional[str]:
        """Best-effort server repo tag (e.g. '0.10.0'); None if undeterminable."""
        try:
            out = subprocess.run(
                ["git", "-C", str(_settings.server_repo_path),
                 "describe", "--tags", "--abbrev=0"],
                capture_output=True, text=True, timeout=5)
            tag = out.stdout.strip().lstrip("v")
            return tag or None
        except (OSError, subprocess.SubprocessError):
            return None
```

Then in `_preflight_and_launch`, change the final line from:

```python
            self._do_launch(entry, port)
```

to:

```python
            self._preflight_apply(entry)
            self._do_launch(entry, port)
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd /Users/tsingletary/code/tt-model-runner && PYTHONPATH=app pytest tests/test_controller.py -k "preflight_applies or remedy" tests/test_controller_contract.py -v`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add app/controller.py tests/test_controller.py tests/test_controller_contract.py
git commit -m "feat: pre-flight workaround application + on_remediation_applied contract"
```

---

## Task 6: Reactive crash → apply → relaunch once

**Files:**
- Modify: `app/controller.py` (the ERROR-hint scan ~line 1026 and/or `_transition(ERROR)` ~line 1099)
- Test: `tests/test_controller.py`

**Interfaces:**
- Consumes: `match_symptom` (Task 2), `_apply_remedy` (Task 4), `restart`/`_do_launch` relaunch pattern.
- Produces: on an ERROR-bound log line matching a KB symptom, apply the remedy and relaunch exactly once (guarded by `self._remediation_attempts < 1`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_controller.py`:

```python
def test_reactive_remediation_relaunches_once(tmp_path, monkeypatch):
    ctrl = make_controller()
    monkeypatch.setattr("controller._settings.server_repo_path", str(tmp_path))
    ctrl._current_entry = make_entry(device_type="P100",
                                     display_name="Llama-3.1-8B-Instruct")
    ctrl._port = "8000"

    relaunches = []
    monkeypatch.setattr(ctrl, "_do_launch",
                        lambda entry, port: relaunches.append(port))
    w = wr.Workaround(id="p100", devices=["P100"], models=["*"],
                      symptom="clash with L1 buffers",
                      env={"MAX_PREFILL_CHUNK_SIZE": "2"}, vllm={"max_model_len": 1024})
    monkeypatch.setattr("controller._wr.match_symptom",
                        lambda line, device, model: w)

    crash = "TT_THROW ... clash with L1 buffers"
    # First crash → apply + relaunch.
    assert ctrl._maybe_remediate(crash) is True
    assert len(relaunches) == 1
    assert ctrl._options.max_model_len == 1024

    # Second crash → capped, no further relaunch.
    assert ctrl._maybe_remediate(crash) is False
    assert len(relaunches) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/tsingletary/code/tt-model-runner && PYTHONPATH=app pytest tests/test_controller.py -k reactive_remediation -v`
Expected: FAIL with `AttributeError: 'AppController' object has no attribute '_maybe_remediate'`

- [ ] **Step 3: Write minimal implementation**

Add to `app/controller.py`:

```python
    def _maybe_remediate(self, line: str) -> bool:
        """If a log line matches a known crash symptom, apply the fix + relaunch.

        Returns True if a reactive relaunch was triggered. Capped at one attempt
        per session (self._remediation_attempts) so we never loop.
        """
        if self._remediation_attempts >= 1:
            return False
        entry = self._current_entry
        port = self._port
        if not entry or not port:
            return False
        try:
            w = _wr.match_symptom(line, entry.device_type, entry.display_name)
        except Exception:
            w = None
        if not w or not w.auto:
            return False
        self._remediation_attempts += 1
        repo_path = Path(_settings.server_repo_path)
        self._apply_remedy(w, repo_path)
        self._emit_remediation_banner(w, preflight=False)
        self._emit("on_log_line", f"↺ Relaunching with workaround {w.id} (attempt 2)…")
        threading.Thread(target=lambda: self._do_launch(entry, port),
                         daemon=True).start()
        return True
```

Then wire it into the existing ERROR-hint scan. In the loop at ~line 1026 that iterates `self._ERROR_HINTS`, add — before or after emitting the hint — a call that lets remediation intercept the same line:

```python
        # Known-issue auto-remediation: try to fix + relaunch before we settle
        # into ERROR. Only fires once per session (see _maybe_remediate).
        if self._maybe_remediate(line):
            return
```

Place this at the point where a line has been identified as error-ish (same
place `self._last_error_hint` is set). Guard so it runs at most once per line
scan and does not fire during a normal READY transition.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/tsingletary/code/tt-model-runner && PYTHONPATH=app pytest tests/test_controller.py -k reactive_remediation -v`
Expected: PASS (both assertions blocks)

- [ ] **Step 5: Run the full controller suite for regressions**

Run: `cd /Users/tsingletary/code/tt-model-runner && PYTHONPATH=app pytest tests/test_controller.py tests/test_controller_contract.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/controller.py tests/test_controller.py
git commit -m "feat: reactive crash->workaround->relaunch (capped at one attempt)"
```

---

## Task 7: `doctor` headless dry-run CLI

**Files:**
- Create: `app/doctor_main.py`
- Modify: `run` (route `--doctor`)
- Test: `tests/test_doctor.py`

**Interfaces:**
- Consumes: `match_preflight` (Task 2), `device_detector.detect_devices` (existing).
- Produces: `doctor_main.run_doctor(argv: list[str]) -> int` — parses `--device`, `--model`, `--json`; prints matches; returns exit code (non-zero when matches exist, zero when clean). `main()` wraps it with `sys.exit`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_doctor.py`:

```python
# SPDX-License-Identifier: Apache-2.0
import json
import doctor_main


def test_doctor_reports_match_and_exits_nonzero(capsys):
    code = doctor_main.run_doctor(["--device", "P100", "--model", "Llama-3.1-8B-Instruct"])
    out = capsys.readouterr().out
    assert "tt-metal#28835" in out
    assert "MAX_PREFILL_CHUNK_SIZE" in out
    assert code != 0                       # known issue present → non-zero


def test_doctor_clean_combo_exits_zero(capsys):
    code = doctor_main.run_doctor(["--device", "N150", "--model", "Qwen3-0.6B"])
    assert code == 0


def test_doctor_json_output_is_valid(capsys):
    code = doctor_main.run_doctor(
        ["--device", "P100", "--model", "Llama-3.1-8B-Instruct", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list) and data
    assert data[0]["id"] == "p100-llama31-8b-l1-prefill"
    assert code != 0


def test_doctor_writes_nothing(tmp_path, monkeypatch):
    # doctor must be mutation-free: no .env created in the cwd/repo.
    monkeypatch.chdir(tmp_path)
    doctor_main.run_doctor(["--device", "P100", "--model", "Llama-3.1-8B-Instruct"])
    assert not (tmp_path / ".env").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/tsingletary/code/tt-model-runner && PYTHONPATH=app pytest tests/test_doctor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'doctor_main'`

- [ ] **Step 3: Write minimal implementation**

Create `app/doctor_main.py`:

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""tt-model-runner doctor — headless dry-run of the known-issue KB.

Reports which workarounds WOULD apply to a device+model. Mutates nothing:
no .env writes, no launches. Inspired by tt-local-generator's `tt-ctl recover`.

    ./run --doctor --device P100 --model Llama-3.1-8B-Instruct
    ./run --doctor --json          # machine-readable
"""
import argparse
import dataclasses
import json
import sys
from typing import List, Optional

import workaround_resolver as wr
import device_detector


def _tty() -> bool:
    return sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty() else text


def run_doctor(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="tt-model-runner --doctor")
    parser.add_argument("--device", help="DeviceType (P100, N150, …); "
                        "auto-detected via tt-smi if omitted")
    parser.add_argument("--model", default="*",
                        help="Model display name (glob-matched)")
    parser.add_argument("--json", action="store_true",
                        help="Emit matched workarounds as JSON")
    args = parser.parse_args(argv)

    devices = [args.device] if args.device else device_detector.detect_devices()
    if not devices:
        print("No device specified and none detected (tt-smi).", file=sys.stderr)
        return 2

    matched: list = []
    for dev in devices:
        for w in wr.match_preflight(dev, args.model, None):
            matched.append((dev, w))

    if args.json:
        print(json.dumps([dataclasses.asdict(w) for _, w in matched], indent=2))
        return 1 if matched else 0

    if not matched:
        print(_c("32", "✓") + f"  No known issues for {', '.join(devices)} + {args.model}")
        return 0

    for dev, w in matched:
        print(_c("33", "●") + f"  {dev} + {args.model}")
        print(f"  known issue {w.ref or w.id}")
        changes = []
        if w.env:
            changes.append(", ".join(f"{k}={v}" for k, v in w.env.items()))
        if "max_model_len" in w.vllm:
            changes.append(f"max_model_len={w.vllm['max_model_len']}")
        if changes:
            print(f"  would apply: {'; '.join(changes)}")
        if w.tradeoff:
            print("  " + _c("33", f"⚠ {w.tradeoff}"))
    # Guidance to stderr so stdout stays parseable.
    print("\n(dry-run — launch the model to auto-apply)", file=sys.stderr)
    return 1


def main() -> None:
    sys.exit(run_doctor(sys.argv[1:]))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Route `--doctor` in `run`**

Edit `run` — change the dispatch block to:

```bash
if [[ "${1:-}" == "--tui" ]]; then
    exec "$PYTHON" "$SCRIPT_DIR/app/tui_main.py" "${@:2}"
elif [[ "${1:-}" == "--doctor" ]]; then
    exec "$PYTHON" "$SCRIPT_DIR/app/doctor_main.py" "${@:2}"
else
    exec "$PYTHON" "$SCRIPT_DIR/app/main.py" "$@"
fi
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /Users/tsingletary/code/tt-model-runner && PYTHONPATH=app pytest tests/test_doctor.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Smoke-test the real entry point**

Run: `cd /Users/tsingletary/code/tt-model-runner && ./run --doctor --device P100 --model Llama-3.1-8B-Instruct; echo "exit=$?"`
Expected: prints the P100 banner and `exit=1`.

- [ ] **Step 7: Commit**

```bash
git add app/doctor_main.py tests/test_doctor.py run
git commit -m "feat: headless --doctor dry-run for known-issue KB (tt-ctl inspired)"
```

---

## Task 8: Wire `on_remediation_applied` into GTK + TUI views

**Files:**
- Modify: `app/main_window.py` (GTK view — register callback)
- Modify: `app/tui/app.py` (TUI view — register callback)

**Interfaces:**
- Consumes: `on_remediation_applied(remedy)` callback emitted by the controller (Tasks 4-6).
- Produces: both views react (at minimum a status/log line; the banner already streams via `on_log_line`).

- [ ] **Step 1: Locate where each view registers `on_*` callbacks**

Run: `cd /Users/tsingletary/code/tt-model-runner && grep -n "on_environment_checked\|controller.on_\|self.controller.on_" app/main_window.py app/tui/app.py`
Expected: shows the block where callbacks like `on_environment_checked` are assigned in each view.

- [ ] **Step 2: Implement the GTK handler**

In `app/main_window.py`, alongside the other `on_*` handler assignments, add:

```python
        controller.on_remediation_applied = self._on_remediation_applied
```

and the method (near the other `_on_*` handlers):

```python
    def _on_remediation_applied(self, remedy):
        """A known-issue workaround was auto-applied — surface a status chip."""
        # The full banner already streamed via on_log_line; here we set a
        # concise, dismissible status so the user knows config was auto-tuned.
        self._set_status(f"⚙ Auto-tuned for known issue {remedy.ref or remedy.id} · "
                         f"undo in Config")
```

> If `MainWindow` has no `_set_status`, use the existing status/toast
> mechanism found in Step 1 (match the pattern already used by, e.g.,
> `on_environment_checked`). Do not invent a new widget.

- [ ] **Step 3: Implement the TUI handler**

In `app/tui/app.py`, alongside the other callback registrations, add:

```python
        controller.on_remediation_applied = self._on_remediation_applied
```

and:

```python
    def _on_remediation_applied(self, remedy):
        """Reflect an auto-applied workaround in the TUI (banner streams via log)."""
        self.notify(f"Auto-tuned for {remedy.ref or remedy.id}", severity="warning")
```

> If the TUI's callback dispatch differs (e.g. handlers live in a widget, or
> use `call_from_thread`), follow the existing pattern from Step 1. `notify`
> is Textual's built-in toast; if unavailable, append to the log pane instead.

- [ ] **Step 4: Verify both apps import and construct cleanly**

Run: `cd /Users/tsingletary/code/tt-model-runner && PYTHONPATH=app python3 -c "import main_window; import tui.app; print('ok')"`
Expected: prints `ok` (no ImportError / AttributeError).

- [ ] **Step 5: Run the full test suite**

Run: `cd /Users/tsingletary/code/tt-model-runner && PYTHONPATH=app pytest tests/ -v`
Expected: PASS (all tests, including the earlier tasks).

- [ ] **Step 6: Commit**

```bash
git add app/main_window.py app/tui/app.py
git commit -m "feat: surface auto-applied workarounds in GTK + TUI views"
```

---

## Self-Review Notes

- **Spec coverage:** KB file (T1), resolver matching + version gate (T2), `_set_env_key` (T3), apply/undo (T4), pre-flight + `on_remediation_applied` contract (T5), reactive retry cap=1 (T6), `doctor` dry-run + `--json` + mutation-free (T7), both views wired (T8). Scope guardrails (no remote sync, no full CLI) are respected — only `doctor` ships.
- **Global constraints honored:** stdlib-only; resolver has no UI imports; banners use left-bar box; reactive cap = 1; `.env` writes logged + undoable; version-gating fails open.
- **Type consistency:** `Workaround` fields, `match_preflight(device, model, repo_version)`, `match_symptom(line, device, model)`, `_apply_remedy(w, repo_path) -> AppliedRemedy`, `AppliedRemedy(remedy, env_keys_written)`, and `on_remediation_applied(remedy)` are used identically across tasks.
- **Known integration risk (flag for executor):** Task 6 Step 3 wires `_maybe_remediate` into the existing error-line scan (`controller.py` ~1026). The executor must read that method's current structure and place the call where a line is confirmed error-ish, before the `ERROR` transition settles — and confirm it does not fire on READY. If the scan's structure differs materially from the spec's assumption, adapt placement while preserving the cap-1 and "relaunch via `_do_launch`" semantics.
