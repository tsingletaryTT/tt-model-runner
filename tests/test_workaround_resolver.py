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
