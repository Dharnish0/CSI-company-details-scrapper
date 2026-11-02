"""``run`` / ``resume`` in slow crawl mode: single-instance lock, signal handlers, banner, exit codes, resume command.
The flag-off path must create no lock file and no signal handler."""
from __future__ import annotations

import json
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

import corpdisc.app as app
from corpdisc.cli import EXIT_INTERRUPTED, EXIT_LOCKED, main
from corpdisc.graphs.supervisor import RunSummary
from corpdisc.services.instance_lock import InstanceLock, lock_path, read_session
from tests.test_cli_status import env  # noqa: F401 - shared fixture (isolated settings, DB_PATH under tmp_path)

ARGS = ["run", "--provider", "fake", "--model", "m", "--slow-crawl"]


def runtime(status="COMPLETED", reason="QUEUE_EMPTY", *, during=None, raises=None):
    class Sup:
        def seed_root(self): ...

        def run(self, max_tasks=None):
            if during:
                during()
            if raises:
                raise raises
            return RunSummary(status=status, stopped_reason=reason)
        resume = run

    return SimpleNamespace(run_id=7, supervisor=Sup(), close=lambda: None)


def patch_runtime(monkeypatch, status="COMPLETED", reason="QUEUE_EMPTY", *, rt=None, **kw):
    seen = {"calls": []}

    def build(settings, **k):
        seen["calls"].append(k)
        return rt if rt is not None else runtime(status, reason, **kw)
    monkeypatch.setattr(app, "build_runtime", build)
    return seen


def db_path(env_):  # noqa: ARG001
    import os
    return os.environ["DB_PATH"]


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


# --------------------------------------------------------------------------------------------- lock
def test_second_live_crawler_is_refused_with_exit_6_and_no_run_is_created(env, monkeypatch, capsys):  # noqa: F811
    seen = patch_runtime(monkeypatch)
    held = InstanceLock(db_path(env))
    held.acquire(run_id=1)
    try:
        assert main(ARGS) == EXIT_LOCKED == 6
        err = capsys.readouterr().err
        assert str(held.pid) in err and str(lock_path(db_path(env))) in err
        assert seen["calls"] == []  # build_runtime (which creates the run) was never reached
        assert read_session(db_path(env))["token"] == held.token  # the live holder's file is untouched
    finally:
        held.release()


def test_stale_lock_of_a_dead_process_is_reclaimed_and_removed_afterwards(env, monkeypatch, capsys):  # noqa: F811
    patch_runtime(monkeypatch)
    pid = dead_pid()
    lock_path(db_path(env)).write_text(json.dumps({"pid": pid, "started_at": "2025-01-01T00:00:00+00:00"}), encoding="utf-8")
    assert main(ARGS) == 0
    assert f"Reclaimed the stale lock of process {pid}" in capsys.readouterr().out
    assert not lock_path(db_path(env)).exists()


def test_lock_is_held_while_running_carries_the_run_id_and_is_released(env, monkeypatch):  # noqa: F811
    seen_during = {}

    def during():
        seen_during["session"] = read_session(db_path(env))
        seen_during["sigint"] = signal.getsignal(signal.SIGINT)
    patch_runtime(monkeypatch, during=during)
    before = signal.getsignal(signal.SIGINT)
    assert main(ARGS) == 0
    assert seen_during["session"]["run_id"] == 7 and seen_during["session"]["state"] in {"STARTING", "RUNNING"}
    assert seen_during["sigint"] is not before  # the graceful-shutdown handler was installed
    assert signal.getsignal(signal.SIGINT) is before  # and restored
    assert not lock_path(db_path(env)).exists()


def test_stop_check_and_lock_reach_build_runtime_only_in_slow_mode(env, monkeypatch):  # noqa: F811
    seen = patch_runtime(monkeypatch)
    assert main(ARGS) == 0
    kw = seen["calls"][0]
    assert callable(kw["stop_check"]) and kw["stop_check"]() is None and isinstance(kw["lock"], InstanceLock)
    seen["calls"].clear()
    assert main(["run", "--provider", "fake", "--model", "m"]) == 0
    assert set(seen["calls"][0]) == {"run_id", "run_provider", "run_model"}  # default call is unchanged


def test_flag_off_creates_no_lock_file_and_no_signal_handler(env, monkeypatch):  # noqa: F811
    seen_during = {}

    def during():
        seen_during["lock"] = lock_path(db_path(env)).exists()
        seen_during["sigint"] = signal.getsignal(signal.SIGINT)
    patch_runtime(monkeypatch, during=during)
    before = signal.getsignal(signal.SIGINT)
    assert main(["run", "--provider", "fake", "--model", "m"]) == 0
    assert seen_during == {"lock": False, "sigint": before}
    assert not lock_path(db_path(env)).exists()


def test_lock_is_released_when_the_runtime_cannot_start(env, monkeypatch, capsys):  # noqa: F811
    def build(settings, **k):
        raise ValueError("boom")
    monkeypatch.setattr(app, "build_runtime", build)
    before = signal.getsignal(signal.SIGINT)
    assert main(ARGS) == 2
    assert "Cannot start: boom" in capsys.readouterr().err
    assert not lock_path(db_path(env)).exists() and signal.getsignal(signal.SIGINT) is before


def test_resume_uses_the_same_lock(env, monkeypatch):  # noqa: F811
    patch_runtime(monkeypatch)
    held = InstanceLock(db_path(env))
    held.acquire(run_id=1)
    try:
        assert main(["resume", "--run-id", "1", "--provider", "fake", "--model", "m", "--slow-crawl", "--yes"]) == 6
    finally:
        held.release()


# --------------------------------------------------------------------------------------------- exit codes and hints
def test_interrupted_exits_5_and_prints_the_exact_resume_command(env, monkeypatch, capsys):  # noqa: F811
    patch_runtime(monkeypatch, "PAUSED", "INTERRUPTED")
    assert main(ARGS) == EXIT_INTERRUPTED == 5
    out = capsys.readouterr().out
    assert "python main.py resume --slow-crawl --run-id 7 --provider fake --model m --yes" in out
    assert "put back on the queue unchanged" in out and "circuit breaker" not in out


def test_second_signal_exception_exits_5_with_the_resume_command(env, monkeypatch, capsys):  # noqa: F811
    patch_runtime(monkeypatch, rt=runtime(raises=KeyboardInterrupt()))
    assert main(ARGS) == 5
    assert "python main.py resume --slow-crawl --run-id 7 --provider fake --model m --yes" in capsys.readouterr().err
    assert not lock_path(db_path(env)).exists()


def test_max_runtime_exits_3_with_hint(env, monkeypatch, capsys):  # noqa: F811
    patch_runtime(monkeypatch, "PAUSED", "MAX_RUNTIME")
    assert main(ARGS) == 3
    out = capsys.readouterr().out
    assert "24-hour runtime limit" in out and "resume --slow-crawl --run-id 7" in out


@pytest.mark.parametrize("reason", ["TOKEN_BUDGET_SLOW_CRAWL_CEILING", "budget: TOKEN_BUDGET_SLOW_CRAWL_COST_CEILING"])
def test_ceiling_stop_exits_3_with_hint(env, monkeypatch, capsys, reason):  # noqa: F811
    patch_runtime(monkeypatch, "BUDGET_EXHAUSTED", reason)
    assert main(ARGS) == 3
    out = capsys.readouterr().out
    assert "ceiling was reached" in out and "SLOW_CRAWL_TOKEN_CEILING" in out and "--slow-crawl --run-id 7" in out


def test_usual_endings_keep_their_exit_codes_and_hints(env, monkeypatch, capsys):  # noqa: F811
    for status, code in [("COMPLETED", 0), ("PARTIAL", 4), ("FAILED", 1)]:
        patch_runtime(monkeypatch, status)
        assert main(ARGS) == code
    out = capsys.readouterr().out
    assert "resume --slow-crawl" not in out


def test_default_mode_hints_are_unchanged(env, monkeypatch, capsys):  # noqa: F811
    patch_runtime(monkeypatch, "PAUSED", "RETRY_WAITING")
    assert main(["run", "--provider", "fake", "--model", "m"]) == 3
    assert "Resume later with: python main.py resume" in capsys.readouterr().out


# --------------------------------------------------------------------------------------------- banner
def test_banner_lists_the_limits_and_never_prints_secrets(env, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setenv("GROQ_API_KEY", "gsk-secret-value")
    monkeypatch.setenv("SLOW_CRAWL_COST_CEILING_USD", "5")
    patch_runtime(monkeypatch)
    assert main(ARGS) == 0
    out = capsys.readouterr()
    text = out.out + out.err
    assert "one provider request at a time" in text and "90s" in text and "24 h" in text
    assert "token ceiling 250000" in text and "cost ceiling $5" in text
    assert "LLM_RPM_LIMIT and LLM_TPM_LIMIT are ignored" in text
    assert "cost ceiling is inactive" in text  # a ceiling with both prices at 0 cannot work
    assert "gsk-secret-value" not in text


def test_banner_has_no_inactive_warning_when_prices_are_set_or_no_cost_ceiling(env, monkeypatch, capsys):  # noqa: F811
    patch_runtime(monkeypatch)
    assert main(ARGS) == 0
    text = capsys.readouterr().out
    assert "cost ceiling none" in text and "inactive" not in text
    monkeypatch.setenv("SLOW_CRAWL_COST_CEILING_USD", "5")
    monkeypatch.setenv("LLM_PRICE_IN_PER_MTOK", "0.5")
    assert main(ARGS) == 0
    text = capsys.readouterr().out
    assert "cost ceiling $5" in text and "inactive" not in text
