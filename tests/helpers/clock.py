"""Deterministic clock and sleeper for tests: sleeping advances time instantly."""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta


class FakeClock:
    def __init__(self, start: datetime | None = None, on_sleep: Callable[[float], None] | None = None) -> None:
        self._now = start or datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        self._mono = 1000.0
        self.sleeps: list[float] = []
        self.on_sleep = on_sleep  # optional hook, called after every sleep with its length (e.g. to raise a stop request)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)
        if self.on_sleep is not None:
            self.on_sleep(seconds)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds

    def advance_to(self, monotonic: float) -> None:
        """Move time forward to the given monotonic reading (never backwards)."""
        if monotonic > self._mono:
            self.advance(monotonic - self._mono)
