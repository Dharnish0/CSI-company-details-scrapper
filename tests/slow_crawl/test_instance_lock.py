"""InstanceLock: single live crawler per database, stale reclaim, session file, no os.kill on Windows."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from corpdisc.services import instance_lock as il
from corpdisc.services.instance_lock import InstanceLock, LockHeld, lock_path, pid_alive, read_session
from tests.helpers.clock import FakeClock


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _write(db, **data):
    lock_path(db).write_text(json.dumps(data), encoding="utf-8")


def test_lock_path_is_next_to_db(tmp_path):
    assert lock_path(tmp_path / "x.db").name == "x.db.slowcrawl.lock"


def test_live_pid_refuses_second_acquire(tmp_path):
    db = tmp_path / "a.db"
    first = InstanceLock(db)
    first.acquire(run_id=3)
    assert read_session(db)["pid"] == os.getpid() and read_session(db)["run_id"] == 3
    second = InstanceLock(db)
    with pytest.raises(LockHeld) as ei:
        second.acquire(run_id=3)
    assert ei.value.pid == os.getpid() and ei.value.path == lock_path(db)
    assert str(lock_path(db)) in str(ei.value) and str(os.getpid()) in str(ei.value)
    assert second.release() is False and lock_path(db).exists()  # a refused instance removes nothing
    assert first.release() is True and not lock_path(db).exists()


def test_old_heartbeat_does_not_override_a_live_pid(tmp_path):
    db = tmp_path / "a.db"
    _write(db, pid=os.getpid(), heartbeat="2000-01-01T00:00:00+00:00")
    os.utime(lock_path(db), (0, 0))
    with pytest.raises(LockHeld):
        InstanceLock(db).acquire()


def test_dead_pid_is_reclaimed(tmp_path):
    db = tmp_path / "a.db"
    dead = _dead_pid()
    assert pid_alive(dead) is False
    _write(db, pid=dead, token="old")
    lock = InstanceLock(db)
    lock.acquire(run_id=7)
    assert lock.reclaimed_from == dead
    s = read_session(db)
    assert s["pid"] == os.getpid() and s["run_id"] == 7 and s["token"] == lock.token


def test_corrupt_fresh_file_refuses_and_old_file_is_reclaimed(tmp_path):
    db = tmp_path / "a.db"
    lock_path(db).write_text("{not json", encoding="utf-8")
    with pytest.raises(LockHeld) as ei:
        InstanceLock(db).acquire()
    assert ei.value.pid is None
    # file mtime is real wall time; the injected clock says it is 2 minutes later
    mtime = lock_path(db).stat().st_mtime
    from datetime import UTC, datetime, timedelta
    clock = FakeClock(datetime.fromtimestamp(mtime, UTC) + timedelta(seconds=120))
    lock = InstanceLock(db, clock=clock)
    lock.acquire()
    assert read_session(db)["pid"] == os.getpid()
    # and just under the grace period it still refuses
    lock.release()
    lock_path(db).write_text("", encoding="utf-8")
    mtime = lock_path(db).stat().st_mtime
    young = FakeClock(datetime.fromtimestamp(mtime, UTC) + timedelta(seconds=30))
    with pytest.raises(LockHeld):
        InstanceLock(db, clock=young).acquire()


def test_release_removes_only_its_own_file(tmp_path):
    db = tmp_path / "a.db"
    lock = InstanceLock(db)
    lock.acquire()
    lock_path(db).unlink()
    _write(db, pid=os.getpid() + 1_000_000, token="someone-else")  # another owner replaced it
    assert lock.release() is False
    assert read_session(db)["token"] == "someone-else"
    assert lock.release() is False  # idempotent


def test_read_session_none_without_file_or_when_corrupt(tmp_path):
    db = tmp_path / "none.db"
    assert read_session(db) is None
    lock_path(db).write_text("[1,2]", encoding="utf-8")
    assert read_session(db) is None


def test_update_merges_and_stamps_heartbeat(tmp_path):
    from datetime import timedelta
    db = tmp_path / "a.db"
    clock = FakeClock()
    lock = InstanceLock(db, clock=clock)
    lock.acquire(run_id=1, tokens_baseline=210_224)
    clock.advance(30)
    assert lock.update(state="SLEEPING", sleeping_until=clock.now() + timedelta(minutes=10)) is True
    s = read_session(db)
    assert s["state"] == "SLEEPING" and s["tokens_baseline"] == 210_224 and s["run_id"] == 1
    assert s["heartbeat"] == "2025-01-01T12:00:30.000000+00:00"
    assert s["sleeping_until"] == "2025-01-01T12:10:30.000000+00:00"
    assert not list(tmp_path.glob("*.tmp"))
    lock.release()
    assert lock.update(state="X") is False  # not held: no resurrection of the file
    assert not lock_path(db).exists()


def test_update_skips_heartbeat_on_permission_error(tmp_path, monkeypatch):
    db = tmp_path / "a.db"
    lock = InstanceLock(db)
    lock.acquire()

    def deny(*_a, **_k):
        raise PermissionError("held open by a reader")

    monkeypatch.setattr(il.os, "replace", deny)
    assert lock.update(state="RUNNING") is False
    assert not list(tmp_path.glob("*.tmp"))
    assert read_session(db)["state"] == "STARTING"


def test_pid_alive_basics_and_no_os_kill_on_windows(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("os.kill must not be used for liveness on Windows")

    if sys.platform == "win32":
        monkeypatch.setattr(il.os, "kill", boom)
    assert pid_alive(os.getpid()) is True
    assert pid_alive(0) is False and pid_alive(-5) is False
    assert pid_alive(_dead_pid()) is False


def test_source_calls_os_kill_only_in_posix_branch():
    src = open(il.__file__, encoding="utf-8").read()
    assert src.count("os.kill(pid, 0)") == 2  # one in the docstring, one in the POSIX path
    code = src.split("def pid_alive")[1].split("def _read_json")[0]
    assert code.index('sys.platform == "win32"') < code.index("os.kill(pid, 0)")
    assert "return" in code.split("os.kill(pid, 0)")[0].split('sys.platform == "win32"')[1]  # Windows branch returns first
