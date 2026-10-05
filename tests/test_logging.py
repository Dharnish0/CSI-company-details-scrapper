from __future__ import annotations

import logging
from pathlib import Path

from corpdisc.config import Settings
from corpdisc.logging_setup import (
    MAX_MESSAGE_CHARS,
    ROOT_LOGGER_NAME,
    get_logger,
    setup_logging,
    shutdown_logging,
)


def read_log(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def managed_handlers() -> list[logging.Handler]:
    handlers = logging.getLogger(ROOT_LOGGER_NAME).handlers
    return [h for h in handlers if getattr(h, "_corpdisc_managed", False)]


def test_creates_log_dir_and_file(tmp_path: Path):
    log_dir = tmp_path / "nested" / "logs"
    s = Settings(_env_file=None, log_dir=log_dir)
    setup_logging(s)
    get_logger("corpdisc.test").info("hello world")
    log_file = log_dir / "corpdisc.log"
    assert log_file.is_file()
    text = read_log(log_file)
    assert "hello world" in text
    assert "[INFO] corpdisc.test: hello world" in text
    # 2026-01-01 12:00:00,123 style timestamp prefix
    assert text[4] == "-" and text[10] == " " and text[19] == ","


def test_custom_log_file_name(tmp_path: Path):
    s = Settings(_env_file=None, log_dir=tmp_path / "logs")
    setup_logging(s, log_file_name="other.log")
    get_logger("x").info("hi")
    assert (tmp_path / "logs" / "other.log").is_file()


def test_get_logger_namespace():
    assert get_logger("abc").name == "corpdisc.abc"
    assert get_logger("corpdisc.cli").name == "corpdisc.cli"
    assert get_logger().name == "corpdisc"


def test_respects_log_level(tmp_path: Path):
    s = Settings(_env_file=None, log_dir=tmp_path, log_level="WARNING")
    setup_logging(s)
    log = get_logger("lvl")
    log.info("quiet info")
    log.warning("loud warning")
    text = read_log(tmp_path / "corpdisc.log")
    assert "quiet info" not in text
    assert "loud warning" in text


def test_idempotent_no_duplicate_handlers(tmp_path: Path):
    s = Settings(_env_file=None, log_dir=tmp_path)
    setup_logging(s)
    first = len(managed_handlers())
    setup_logging(s)
    setup_logging(s)
    assert len(managed_handlers()) == first == 2
    get_logger("dup").info("only once")
    assert read_log(tmp_path / "corpdisc.log").count("only once") == 1


def test_shutdown_removes_handlers(tmp_path: Path):
    setup_logging(Settings(_env_file=None, log_dir=tmp_path))
    shutdown_logging()
    assert managed_handlers() == []


def test_masks_configured_secrets(tmp_path: Path):
    s = Settings(_env_file=None, log_dir=tmp_path, llm_api_key="fake-llm-key-ABC123")
    setup_logging(s)
    log = get_logger("sec")
    log.info("using key fake-llm-key-ABC123 now")
    log.info("formatted %s", "fake-llm-key-ABC123")
    text = read_log(tmp_path / "corpdisc.log")
    assert "fake-llm-key-ABC123" not in text
    assert "***" in text


def test_masks_secret_patterns(tmp_path: Path):
    setup_logging(Settings(_env_file=None, log_dir=tmp_path))
    log = get_logger("pat")
    log.info("request api_key=abc123xyz&x=1")
    log.info("LLM_API_KEY: topsecretvalue")
    log.info("header Authorization: Bearer abc.def-ghi")
    log.info("token=%s password='p w'", "tok123")
    text = read_log(tmp_path / "corpdisc.log")
    for leaked in ("abc123xyz", "topsecretvalue", "abc.def-ghi", "tok123", "p w"):
        assert leaked not in text
    assert "x=1" in text


def test_masks_secrets_in_exception_text(tmp_path: Path):
    s = Settings(_env_file=None, log_dir=tmp_path, search_api_key="fake-search-key-999")
    setup_logging(s)
    try:
        raise ValueError("bad call with fake-search-key-999")
    except ValueError:
        get_logger("exc").exception("failed")
    text = read_log(tmp_path / "corpdisc.log")
    assert "Traceback" in text
    assert "fake-search-key-999" not in text


def test_truncates_oversized_messages(tmp_path: Path):
    setup_logging(Settings(_env_file=None, log_dir=tmp_path))
    body = "x" * (MAX_MESSAGE_CHARS + 3000)
    get_logger("big").info("page body: %s", body)
    text = read_log(tmp_path / "corpdisc.log")
    assert "...[truncated " in text
    assert len(text) < MAX_MESSAGE_CHARS + 500
    assert "x" * (MAX_MESSAGE_CHARS + 1) not in text
