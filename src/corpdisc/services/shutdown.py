"""Graceful shutdown for slow-crawl mode (plan decision D11).

The first SIGINT / SIGTERM / SIGBREAK sets a flag: the request in flight finishes, no new provider request starts, and
the supervisor puts the running task back on the queue. A second signal raises ``KeyboardInterrupt`` so a stuck process can
still be stopped from the keyboard. Handlers can only be installed from the main thread.
"""

from __future__ import annotations

import signal
import threading
from types import FrameType
from typing import Any, Final

SHUTDOWN: Final = "SHUTDOWN"


class ShutdownFlag:
    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason: str | None = None
        self._previous: dict[int, Any] = {}

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def request(self, reason: str = SHUTDOWN) -> None:
        """Ask for a graceful stop. A repeated request while one is pending is a no-op here (the signal handler escalates)."""
        if not self._event.is_set():
            self.reason = reason
            self._event.set()

    def stop_check(self) -> str | None:
        """For ``SlowCrawlLimiter(stop_check=...)``: ``"SHUTDOWN"`` once a stop was requested, else None."""
        return SHUTDOWN if self._event.is_set() else None

    # ---------------------------------------------------------------- signals
    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        if self._event.is_set():
            raise KeyboardInterrupt(f"second signal {signum}: stopping now")
        self.request(SHUTDOWN)

    def install(self) -> bool:
        """Register SIGINT, SIGTERM and SIGBREAK (where present). Returns False outside the main thread."""
        if threading.current_thread() is not threading.main_thread():
            return False
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None or int(sig) in self._previous:
                continue
            self._previous[int(sig)] = signal.signal(sig, self._handle)
        return True

    def restore(self) -> None:
        for signum, prev in list(self._previous.items()):
            signal.signal(signum, prev)
        self._previous.clear()
