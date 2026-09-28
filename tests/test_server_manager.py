import sys
from pathlib import Path
import pytest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
from server_manager import LogParser, ServerState, LaunchConfig, ServerManager
from launch_options import LaunchOptions


def test_parse_pulling_image():
    p = LogParser()
    assert p.feed("2026-04-18 12:00:01 INFO: docker pull ghcr.io/tenstorrent/...") == ServerState.PULLING_IMAGE


def test_parse_loading_vllm():
    p = LogParser()
    s = p.feed("INFO 04-18 12:01:05 config.py:100] Automatically detected platform: tt")
    assert s == ServerState.LOADING


def test_parse_device_setup_substage():
    p = LogParser()
    p.feed("INFO multidevice with 4 devices successfully created")
    assert p.last_substage == "device_setup"


def test_parse_trace_capture_progress():
    p = LogParser()
    p.feed("INFO Capturing traces: input_seq_len=512")
    assert p.trace_capture_count == 3   # 512 is 3rd in [128,256,512,...]
    assert p.last_substage == "trace_capture"


def test_trace_capture_all_10_lengths():
    p = LogParser()
    lengths = [128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32640, 65408]
    for i, l in enumerate(lengths, 1):
        p.feed(f"INFO Capturing traces: input_seq_len={l}")
        assert p.trace_capture_count == i


def test_parse_warmup_progress():
    p = LogParser()
    p.feed("50%|█████     | 1/2 [00:41<00:41, 41.00s/it]")
    assert p.warmup_n == 1
    assert p.warmup_total == 2
    assert p.last_substage == "warmup"


def test_parse_error():
    p = LogParser()
    s = p.feed("ERROR: Container exited with code 1")
    assert s == ServerState.ERROR


def test_parse_media_cache_substage():
    p = LogParser()
    p.feed("loading cache at /data/tt_metal_cache/transformer/")
    assert p.last_substage == "cache_loading"


def test_parse_warmup_complete():
    p = LogParser()
    p.feed("All devices are warmed up and ready to serve")
    assert p.last_substage == "warmup_complete"


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
        # Mock the process to have stderr and poll() that returns a non-None value (exited)
        mock_popen.return_value.stderr = iter([])
        mock_popen.return_value.poll.return_value = 0
        mgr.launch(config, lambda l: None, states.append)

    mock_popen.assert_called_once()
    # Check that Popen was called with --override-docker-image in the args
    call_args = mock_popen.call_args[0][0]
    assert "--override-docker-image" in call_args
    assert "ghcr.io/tt/dev:latest" in call_args
    # Should not have emitted ERROR for the dev_mode check (though may emit ERROR for other reasons)
    # The key is that we proceed to call Popen, meaning the guard didn't trip
    assert mock_popen.call_count == 1
