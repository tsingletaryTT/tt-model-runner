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


def _kb():
    return [
        wr.Workaround(
            id="p100", devices=["P100", "P150"], models=["Llama-3.1-8B*"],
            symptom="clash with L1 buffers", env={"MAX_PREFILL_CHUNK_SIZE": "2"},
            vllm={"max_model_len": 1024}, applies_to_versions="<=0.10.0",
        ),
        wr.Workaround(id="anydev", devices=[], models=[], symptom="buffers"),
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


def test_load_caches_parsed_kb(tmp_path):
    """load_workarounds caches per path: match_preflight/match_symptom run hot,
    and the bundled KB never changes within a session, so a second call for the
    same path returns the cached parse rather than re-reading disk."""
    kb = tmp_path / "cache_test.json"
    kb.write_text('[{"id": "first"}]')
    first = wr.load_workarounds(kb)
    assert [w.id for w in first] == ["first"]

    kb.write_text('[{"id": "second"}]')          # change on disk after first load
    again = wr.load_workarounds(kb)
    assert [w.id for w in again] == ["first"]     # served from cache, not re-parsed
