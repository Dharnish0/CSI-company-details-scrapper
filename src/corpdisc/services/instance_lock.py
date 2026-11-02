"""Single-instance guard and session file for slow-crawl mode (plan decision D12).

``<db_path>.slowcrawl.lock`` is created with ``O_CREAT | O_EXCL`` and holds JSON (pid, started_at, run_id and the live
session state: state, sleeping_until, heartbeat, baselines). The same file is the session file read by ``progress``.

* A lock whose pid is alive is never overridden, even if its heartbeat is old (a suspended laptop must not get a
  second crawler). ``LockHeld`` names the pid and the file so the user can remove it by hand.
* A dead pid, or an unreadable file older than ``CORRUPT_GRACE_SECONDS``, is stale and reclaimed.
* Liveness on Windows uses ``OpenProcess`` / ``GetExitCodeProcess``. ``os.kill(pid, 0)`` is used on POSIX only: on
  Windows it would terminate the target process.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from corpdisc.clock import Clock, SystemClock, iso_utc

LOCK_SUFFIX: Final = ".slowcrawl.lock"
CORRUPT_GRACE_SECONDS: Final = 60.0
_PROCESS_QUERY_LIMITED_INFORMATION: Final = 0x1000
_STILL_ACTIVE: Final = 259
_ERROR_ACCESS_DENIED: Final = 5


class LockHeld(RuntimeError):
    """Another live slow-crawl process owns the lock (``pid`` is None when the file is unreadable but fresh)."""

    def __init__(self, pid: int | None, path: Path) -> None:
        self.pid, self.path = pid, path
        who = f"process {pid}" if pid is not None else "another process (lock file unreadable, written moments ago)"
        super().__init__(f"slow crawl is already running: held by {who}. Lock file: {path}. "
                         "If that process is really gone, delete the lock file and retry.")


def lock_path(db_path: str | Path) -> Path:
    return Path(str(db_path) + LOCK_SUFFIX)


def pid_alive(pid: int) -> bool:
    """True when ``pid`` is a running process. Never signals the process on Windows."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not handle:
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED  # exists but not ours to query
        try:
            code = ctypes.c_uint32()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # POSIX only: signal 0 checks existence
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_session(db_path: str | Path) -> dict[str, Any] | None:
    """The session JSON next to ``db_path``, or None when there is no readable lock file."""
    return _read_json(lock_path(db_path))


class InstanceLock:
    def __init__(self, db_path: str | Path, *, clock: Clock | None = None, pid: int | None = None) -> None:
        self.path = lock_path(db_path)
        self._clock: Clock = clock or SystemClock()
        self.pid = os.getpid() if pid is None else pid
        self.token = uuid.uuid4().hex
        self._data: dict[str, Any] = {}
        self.held = False
        self.reclaimed_from: int | None = None  # pid of the stale holder that was replaced, for the startup log line

    # ---------------------------------------------------------------- acquire / release
    def acquire(self, run_id: int | None = None, **fields: Any) -> None:
        """Create the lock or raise ``LockHeld``. Stale locks are reclaimed."""
        self._data = {"pid": self.pid, "token": self.token, "started_at": iso_utc(self._clock.now()), "run_id": run_id,
                      "state": "STARTING", **fields}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(4):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                self._reclaim_or_raise()
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(self._data, default=str))
            self.held = True
            return
        raise LockHeld(None, self.path)  # lost the creation race repeatedly: someone else is starting

    def _reclaim_or_raise(self) -> None:
        existing = _read_json(self.path)
        pid = existing.get("pid") if existing else None
        if isinstance(pid, int) and not isinstance(pid, bool):
            if pid_alive(pid):
                raise LockHeld(pid, self.path)
            self.reclaimed_from = pid
        else:
            try:
                mtime = datetime.fromtimestamp(self.path.stat().st_mtime, UTC)
            except FileNotFoundError:
                return  # vanished meanwhile: the caller retries the create
            if (self._clock.now() - mtime).total_seconds() < CORRUPT_GRACE_SECONDS:
                raise LockHeld(None, self.path)
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    def _owns_file(self) -> bool:
        cur = _read_json(self.path)
        return bool(cur) and cur.get("pid") == self.pid and cur.get("token") == self.token

    def release(self) -> bool:
        """Remove the lock file only when this instance wrote it. Returns True when a file was removed."""
        if not self.held:
            return False
        self.held = False
        if not self._owns_file():
            return False
        try:
            self.path.unlink()
        except OSError:
            return False
        return True

    # ----------------------------------------------------------------------- updates
    def update(self, **fields: Any) -> bool:
        """Merge ``fields`` into the session file (atomic replace). ``heartbeat`` defaults to now. Returns False when the
        file could not be replaced (Windows readers can hold it open), in which case this heartbeat is skipped."""
        if not self.held:
            return False
        fields.setdefault("heartbeat", self._clock.now())
        for k, v in fields.items():
            self._data[k] = iso_utc(v) if isinstance(v, datetime) else v
        tmp = self.path.with_name(self.path.name + f".{self.pid}.tmp")
        try:
            tmp.write_text(json.dumps(self._data, default=str), encoding="utf-8")
            os.replace(tmp, self.path)
        except PermissionError:
            with contextlib.suppress(OSError):
                tmp.unlink()
            return False
        return True

    def __enter__(self) -> InstanceLock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
