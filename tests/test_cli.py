from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from corpdisc.cli import main
from corpdisc.db.connection import verify_db

from .conftest import CONFIG_ENV_VARS, PROJECT_ROOT

MAIN_PY = PROJECT_ROOT / "main.py"


def clean_env(**overrides: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() not in CONFIG_ENV_VARS}
    env.update(overrides)
    return env


def test_help_in_process(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as info:
        main(["--help"])
    assert info.value.code == 0
    out = capsys.readouterr().out
    assert "init-db" in out
    for forbidden in ("crawl", "export", "llm"):
        assert f"\n    {forbidden}" not in out


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]):
    assert main([]) == 0
    assert "init-db" in capsys.readouterr().out


def test_unknown_command_fails():
    with pytest.raises(SystemExit) as info:
        main(["crawl"])
    assert info.value.code == 2


def test_init_db_in_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                            capsys: pytest.CaptureFixture[str]):
    db = tmp_path / "data" / "cli.db"
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    assert main(["init-db"]) == 0
    out = capsys.readouterr().out
    assert "Database initialized" in out and "17 tables" in out
    assert "foreign_keys=ON" in out and "journal_mode=wal" in out
    assert db.is_file()
    assert verify_db(db).table_count == 17
    assert (tmp_path / "logs" / "corpdisc.log").is_file()
    assert main(["init-db"]) == 0  # idempotent


def test_init_db_invalid_config_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                      capsys: pytest.CaptureFixture[str]):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "never.db"))
    monkeypatch.setenv("ROOT_CODE", "bad code")
    assert main(["init-db"]) != 0
    err = capsys.readouterr().err
    assert "ROOT_CODE" in err
    assert not (tmp_path / "never.db").exists()


def test_init_db_db_error_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                capsys: pytest.CaptureFixture[str]):
    blocker = tmp_path / "file.txt"
    blocker.write_text("x")
    monkeypatch.setenv("DB_PATH", str(blocker / "sub" / "x.db"))  # parent is a file
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    assert main(["init-db"]) != 0
    assert "failed" in capsys.readouterr().err.lower()


def test_network_guard_blocks_sockets():
    import socket

    with pytest.raises(RuntimeError, match="network access"):
        socket.create_connection(("example.com", 80), timeout=1)


def test_help_subprocess():
    proc = subprocess.run([sys.executable, str(MAIN_PY), "--help"], cwd=PROJECT_ROOT,
                          env=clean_env(), capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0
    assert "init-db" in proc.stdout


def test_init_db_subprocess(tmp_path: Path):
    db = tmp_path / "sub" / "proc.db"
    env = clean_env(DB_PATH=str(db), LOG_DIR=str(tmp_path / "logs"))
    proc = subprocess.run([sys.executable, str(MAIN_PY), "init-db"], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "Database initialized" in proc.stdout and "17 tables" in proc.stdout
    assert db.is_file()


def test_init_db_subprocess_invalid_config(tmp_path: Path):
    env = clean_env(DB_PATH=str(tmp_path / "x.db"), LOG_DIR=str(tmp_path / "logs"),
                    ROOT_CODE="bad code")
    proc = subprocess.run([sys.executable, str(MAIN_PY), "init-db"], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0
    assert "ROOT_CODE" in proc.stderr
