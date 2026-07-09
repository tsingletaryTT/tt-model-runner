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


def test_doctor_json_includes_device(capsys):
    # --json must carry the matched device so consumers can tell which device
    # each workaround applied to (matters when devices are auto-detected).
    code = doctor_main.run_doctor(
        ["--device", "P100", "--model", "Llama-3.1-8B-Instruct", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data[0]["device"] == "P100"
    assert data[0]["id"] == "p100-llama31-8b-l1-prefill"
    assert code != 0


def test_doctor_requires_model():
    # --model is required: defaulting to "*" silently matched no KB pattern, so
    # the flag is now mandatory (argparse exits non-zero when it is missing).
    import pytest
    with pytest.raises(SystemExit):
        doctor_main.run_doctor(["--device", "P100"])
