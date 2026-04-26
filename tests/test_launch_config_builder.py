# tests/test_launch_config_builder.py
import os
from pathlib import Path
from unittest.mock import patch

from launch_config_builder import build_launch_config
from launch_options import LaunchOptions
from app_settings import AppSettings


def _settings(tmp_path, **kw) -> AppSettings:
    s = AppSettings(config_dir=str(tmp_path))
    s.server_repo_path = str(tmp_path)
    s.hf_cache_path = ""
    s.host_weights_dir = ""
    s.cache_root_path = str(tmp_path / "cache")
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_hf_token_from_env(tmp_path):
    with patch.dict(os.environ, {"HF_TOKEN": "tok123"}):
        cfg = build_launch_config("M", "N150", "8000", "0", "vllm",
                                  _settings(tmp_path), LaunchOptions())
    assert cfg.hf_token == "tok123"


def test_hf_token_from_dotenv(tmp_path):
    (tmp_path / ".env").write_text("HF_TOKEN=dotenv_tok\n")
    env = {k: v for k, v in os.environ.items() if k != "HF_TOKEN"}
    with patch.dict(os.environ, env, clear=True):
        cfg = build_launch_config("M", "N150", "8000", "0", "vllm",
                                  _settings(tmp_path), LaunchOptions())
    assert cfg.hf_token == "dotenv_tok"


def test_hf_token_missing_is_none(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "HF_TOKEN"}
    with patch.dict(os.environ, env, clear=True):
        cfg = build_launch_config("M", "N150", "8000", "0", "vllm",
                                  _settings(tmp_path), LaunchOptions())
    assert cfg.hf_token is None


def test_weights_dir_preferred(tmp_path):
    wd = tmp_path / "weights"
    wd.mkdir()
    cfg = build_launch_config("M", "N150", "8000", "0", "vllm",
                               _settings(tmp_path, host_weights_dir=str(wd)), LaunchOptions())
    assert cfg.options.host_weights_dir == str(wd)
    assert not cfg.options.host_hf_cache
    assert not cfg.options.host_volume


def test_hf_cache_fallback(tmp_path):
    hf = tmp_path / "hf"
    hf.mkdir()
    cfg = build_launch_config("M", "N150", "8000", "0", "vllm",
                               _settings(tmp_path, hf_cache_path=str(hf)), LaunchOptions())
    assert cfg.options.host_hf_cache == str(hf)
    assert not cfg.options.host_volume


def test_volume_fallback(tmp_path):
    cfg = build_launch_config("M", "N150", "8000", "0", "vllm",
                               _settings(tmp_path), LaunchOptions())
    assert cfg.options.host_volume


def test_device_id_set(tmp_path):
    cfg = build_launch_config("M", "N150", "8002", "2", "vllm",
                               _settings(tmp_path), LaunchOptions())
    assert cfg.options.device_id == "2"


def test_does_not_mutate_original_options(tmp_path):
    opts = LaunchOptions()
    build_launch_config("M", "N150", "8000", "0", "vllm", _settings(tmp_path), opts)
    assert opts.device_id == ""


def test_config_fields(tmp_path):
    cfg = build_launch_config("MyModel", "P300X2", "8003", "0,1", "media",
                               _settings(tmp_path), LaunchOptions())
    assert cfg.model_name == "MyModel"
    assert cfg.device == "P300X2"
    assert cfg.port == "8003"
    assert cfg.inference_engine == "media"
    assert cfg.no_auth is True
