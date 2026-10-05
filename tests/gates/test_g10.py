"""Tests for G10 — Cycle."""

import pytest

from corpdisc.gates import check_g10_cycle
from corpdisc.services.cycle_guard import CycleGuard


class TestG10Cycle:
    """Tests for G10 — Cycle."""

    def test_g10_valid_relationship(self) -> None:
        """Valid parent-child relationship should be allowed."""
        conn = pytest.importorskip("sqlite3").connect(":memory:")
        conn.execute("CREATE TABLE relationships (parent_id INTEGER, child_id INTEGER, verification_status TEXT)")
        guard = CycleGuard(conn)
        result = check_g10_cycle(guard, parent_id=1, child_id=2, ancestry=())
        assert result.allowed is True

    def test_g10_self_loop_rejected(self) -> None:
        """Self-loop should be rejected with CYCLE."""
        conn = pytest.importorskip("sqlite3").connect(":memory:")
        conn.execute("CREATE TABLE relationships (parent_id INTEGER, child_id INTEGER, verification_status TEXT)")
        guard = CycleGuard(conn)
        result = check_g10_cycle(guard, parent_id=1, child_id=1, ancestry=())
        assert result.allowed is False
        assert result.reason == "CYCLE"

    def test_g10_child_in_ancestry_rejected(self) -> None:
        """Child in ancestry should be rejected with CYCLE."""
        conn = pytest.importorskip("sqlite3").connect(":memory:")
        conn.execute("CREATE TABLE relationships (parent_id INTEGER, child_id INTEGER, verification_status TEXT)")
        guard = CycleGuard(conn)
        result = check_g10_cycle(guard, parent_id=2, child_id=1, ancestry=(1, 2))
        assert result.allowed is False
        assert result.reason == "CYCLE"

    def test_g10_cycle_via_existing_edge(self) -> None:
        """Cycle via existing edge should be rejected."""
        conn = pytest.importorskip("sqlite3").connect(":memory:")
        conn.execute("CREATE TABLE relationships (parent_id INTEGER, child_id INTEGER, verification_status TEXT)")
        # Create existing edge: 1 -> 2
        conn.execute("INSERT INTO relationships VALUES (1, 2, 'CONFIRMED')")
        conn.commit()
        guard = CycleGuard(conn)
        # Try to add: 2 -> 1 (would create cycle)
        result = check_g10_cycle(guard, parent_id=2, child_id=1, ancestry=())
        assert result.allowed is False
        assert result.reason == "CYCLE"
