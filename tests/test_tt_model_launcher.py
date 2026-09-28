import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
from tt_model_launcher import TtModelLauncher, TtModelLaunchConfig
from server_manager import ServerState


def _fake_popen(lines, returncode=0):
    proc = MagicMock()
    proc.stdout = iter(lines)
    proc.wait.return_value = returncode
    return proc


def test_launch_builds_expected_command_and_reports_launching():
    launcher = TtModelLauncher()
    log_lines, states = [], []
    with patch("tt_model_launcher.shutil.which", return_value="/usr/bin/tt"), \
         patch("tt_model_launcher.subprocess.Popen") as mock_popen:
        mock_popen.return_value = _fake_popen(["Starting vLLM API server\n"])
        launcher.launch(
            TtModelLaunchConfig(bundle_id="acme/llama-fast", port="8001"),
            log_lines.append, states.append,
        )
        launcher._thread.join(timeout=5)

    args = mock_popen.call_args[0][0]
    assert args == ["/usr/bin/tt", "serve", "acme/llama-fast", "--port", "8001"]
    assert ServerState.LAUNCHING in states
    assert ServerState.LOADING in states  # "Starting vLLM API server" → LOADING via LogParser


def test_launch_includes_profile_flag_when_set():
    launcher = TtModelLauncher()
    with patch("tt_model_launcher.shutil.which", return_value="/usr/bin/tt"), \
         patch("tt_model_launcher.subprocess.Popen") as mock_popen:
        mock_popen.return_value = _fake_popen([])
        launcher.launch(
            TtModelLaunchConfig(bundle_id="acme/llama-fast", port="8001", profile="fast"),
            lambda l: None, lambda s: None,
        )
        launcher._thread.join(timeout=5)

    args = mock_popen.call_args[0][0]
    assert args == ["/usr/bin/tt", "serve", "acme/llama-fast", "--port", "8001", "--profile", "fast"]


def test_launch_reports_error_when_tt_missing():
    launcher = TtModelLauncher()
    log_lines, states = [], []
    with patch("tt_model_launcher.shutil.which", return_value=None):
        launcher.launch(
            TtModelLaunchConfig(bundle_id="acme/llama-fast", port="8001"),
            log_lines.append, states.append,
        )
        launcher._thread.join(timeout=5)

    assert ServerState.ERROR in states
    assert any("tt not found" in l for l in log_lines)


def test_launch_reports_error_on_nonzero_exit():
    launcher = TtModelLauncher()
    states = []
    with patch("tt_model_launcher.shutil.which", return_value="/usr/bin/tt"), \
         patch("tt_model_launcher.subprocess.Popen") as mock_popen:
        mock_popen.return_value = _fake_popen(["some error\n"], returncode=1)
        launcher.launch(
            TtModelLaunchConfig(bundle_id="acme/llama-fast", port="8001"),
            lambda l: None, states.append,
        )
        launcher._thread.join(timeout=5)

    assert ServerState.ERROR in states


def test_stop_calls_tt_model_stop_with_bundle_id():
    launcher = TtModelLauncher()
    with patch("tt_model_launcher.shutil.which", return_value="/usr/bin/tt"), \
         patch("tt_model_launcher.subprocess.Popen") as mock_popen, \
         patch("tt_model_launcher.subprocess.run") as mock_run:
        mock_popen.return_value = _fake_popen([])
        launcher.launch(
            TtModelLaunchConfig(bundle_id="acme/llama-fast", port="8001"),
            lambda l: None, lambda s: None,
        )
        launcher._thread.join(timeout=5)
        launcher.stop()

    stop_args = mock_run.call_args[0][0]
    assert stop_args == ["/usr/bin/tt", "model", "stop", "acme/llama-fast"]


def test_stop_clears_bundle_id_so_later_stops_do_not_rerun_tt_model_stop():
    launcher = TtModelLauncher()
    with patch("tt_model_launcher.shutil.which", return_value="/usr/bin/tt"), \
         patch("tt_model_launcher.subprocess.Popen") as mock_popen, \
         patch("tt_model_launcher.subprocess.run") as mock_run:
        mock_popen.return_value = _fake_popen([])
        launcher.launch(
            TtModelLaunchConfig(bundle_id="acme/llama-fast", port="8001"),
            lambda l: None, lambda s: None,
        )
        launcher._thread.join(timeout=5)
        launcher.stop()
        launcher.stop()   # e.g. a later stop() for an unrelated launch

    assert mock_run.call_count == 1
    assert launcher._bundle_id is None
