"""Community bundles must stay in their own UI section (fix wave 1, #3).

Community entries share ModelCatalog._entries with tt-inference-server
entries (distinguished only by `source`), so every "all entries" UI surface
has to filter them out explicitly:

- TUI ModelRail.load_catalog must not put them in the main #model-list
  (they belong only in #community-list via load_community_entries).
- GTK Sidebar._on_tree_selection must not persist a community entry's
  tt-cli hardware tag as settings.last_device.

The GTK test needs PyGObject (gi) and skips cleanly without it — run it with
the system interpreter (e.g. `PYTHONPATH=app /usr/bin/python3 -m pytest
tests/test_community_ui_isolation.py`) to exercise it.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
from model_catalog import ModelCatalog, ModelEntry


def _entry(name, device, source):
    return ModelEntry(
        model_id=name, model_name=name, display_name=name.split("/")[-1],
        hf_model_repo=name, model_type="LLM" if source == "inference_server" else "COMMUNITY",
        family="x", device_type=device, inference_engine="vllm", docker_image="",
        status="COMPLETE", param_count=None, min_disk_gb=None, min_ram_gb=None,
        source=source,
    )


def _mixed_catalog():
    cat = ModelCatalog([_entry("meta/llama", "P300X2", "inference_server")])
    # Same device_type as the curated entry, so the hw-compat filter would
    # "coincidentally" let it through if it weren't excluded by source.
    cat.merge_community([_entry("org/bundle", "P300X2", "community")])
    return cat


def test_tui_model_rail_main_list_excludes_community_entries():
    pytest.importorskip("textual")
    from tui.widgets.model_rail import ModelRail

    rail = ModelRail()
    # Stub the methods that touch mounted widgets; we only care about which
    # entries feed the main list.
    for name in ("_repopulate_model_list", "_refresh_starred_recent",
                 "_scan_hf_cache_async", "_update_hw_filter_indicator"):
        setattr(rail, name, lambda *a, **k: None)

    for hw_filter in (True, False):
        rail._hw_filter = hw_filter
        rail.load_catalog(_mixed_catalog(), ["P300X2"])
        ids = [e.model_id for e in rail._entries]
        assert ids == ["meta/llama"], (hw_filter, ids)
        assert all(e.source != "community" for e in rail._raw_entries)


def _run_gtk_selection(entry_name, device, settings):
    main_window = pytest.importorskip("main_window")
    cat = _mixed_catalog()
    selected = []
    fake_self = SimpleNamespace(
        _compat_catalog=None, on_compat_select=None, _catalog=cat,
        _selected_entry=None, _on_model_select=selected.append,
    )
    values = {1: entry_name, 2: device, 3: True}
    model = MagicMock()
    model.get_value.side_effect = lambda it, col: values[col]
    sel = MagicMock()
    sel.get_selected.return_value = (model, object())
    with patch("main_window._settings", settings):
        main_window.Sidebar._on_tree_selection(fake_self, sel)
    return selected


def test_gtk_selecting_community_entry_does_not_persist_last_device(tmp_path):
    pytest.importorskip("gi")
    from app_settings import AppSettings
    settings = AppSettings(config_dir=tmp_path)
    settings.last_device = "N150"
    selected = _run_gtk_selection("org/bundle", "P300X2", settings)
    assert [e.model_id for e in selected] == ["org/bundle"]
    assert settings.last_device == "N150"


def test_gtk_selecting_curated_entry_still_persists_last_device(tmp_path):
    pytest.importorskip("gi")
    from app_settings import AppSettings
    settings = AppSettings(config_dir=tmp_path)
    settings.last_device = "N150"
    _run_gtk_selection("meta/llama", "P300X2", settings)
    assert settings.last_device == "P300X2"
