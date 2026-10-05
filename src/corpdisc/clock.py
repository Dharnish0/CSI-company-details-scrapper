"""Neutral time source: the ONLY module allowed to read the wall clock or sleep.

Every service takes a ``Clock`` (and a ``Sleeper`` where it waits), so tests can
inject a fake and never sleep. Timestamps are stored as ISO-8601 UTC text.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Aware UTC datetime."""
        ...

    def monotonic(self) -> float: ...


@runtime_checkable
class Sleeper(Protocol):
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """Real clock and sleeper."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


def iso_utc(dt: datetime) -> str:
    """Format an aware datetime as ISO-8601 UTC with microseconds and a 'Z'-free +00:00 offset."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def parse_iso_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
