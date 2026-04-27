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
