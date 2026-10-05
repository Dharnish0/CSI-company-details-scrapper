"""Tests for G1 — ID Integrity."""

import pytest

from corpdisc.gates import check_g1_id_integrity, GateResult


class TestG1IdIntegrity:
    """Tests for G1 — ID Integrity."""

    def test_g1_valid_id_allowed(self) -> None:
        """Known IDs should be allowed."""
        known_ids = frozenset({1, 2, 3})
        result = check_g1_id_integrity("1", known_ids)
        assert result.allowed is True
        assert result.reason is None
        assert result.detail is None

    def test_g1_unknown_id_rejected(self) -> None:
        """Unknown IDs should be rejected with UNKNOWN_ID."""
        known_ids = frozenset({1, 2, 3})
        result = check_g1_id_integrity("999", known_ids)
        assert result.allowed is False
        assert result.reason == "UNKNOWN_ID"
        assert "999" in result.detail or "unknown" in result.detail.lower()

    def test_g1_invalid_id_format(self) -> None:
        """Non-numeric IDs should be rejected."""
        known_ids = frozenset({1, 2, 3})
        result = check_g1_id_integrity("not_a_number", known_ids)
        assert result.allowed is False
        assert result.reason == "UNKNOWN_ID"
        assert "not_a_number" in result.detail

    def test_g1_boundary_id(self) -> None:
        """IDs at boundaries should work correctly."""
        known_ids = frozenset({0, 100})
        result = check_g1_id_integrity("0", known_ids)
        assert result.allowed is True

        result = check_g1_id_integrity("100", known_ids)
        assert result.allowed is True

        result = check_g1_id_integrity("50", known_ids)
        assert result.allowed is False
        assert result.reason == "UNKNOWN_ID"
