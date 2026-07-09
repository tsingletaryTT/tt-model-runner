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
