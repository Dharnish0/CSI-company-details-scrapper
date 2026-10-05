"""Tests for G6 — Reachability."""

from dataclasses import dataclass

from corpdisc.gates import check_g6_reachability


@dataclass(frozen=True)
class MockFetchResult:
    """Mock fetch result for testing."""
    status: str
    text: str | None = None
    error: str | None = None


class TestG6Reachability:
    """Tests for G6 — Reachability."""

    def test_g6_reachable_ok(self) -> None:
        """Successful fetch should be allowed."""
        fetch_result = MockFetchResult(status="OK", text="Page content")
        result = check_g6_reachability(fetch_result)
        assert result.allowed is True

    def test_g6_reachable_cached(self) -> None:
        """Cached fetch should be allowed."""
        fetch_result = MockFetchResult(status="CACHED", text="Cached content")
        result = check_g6_reachability(fetch_result)
        assert result.allowed is True

    def test_g6_unreachable_http_error(self) -> None:
        """HTTP error should be rejected with UNREACHABLE."""
        fetch_result = MockFetchResult(status="HTTP_ERROR", text=None, error="404 Not Found")
        result = check_g6_reachability(fetch_result)
        assert result.allowed is False
        assert result.reason == "UNREACHABLE"
        assert "404" in result.detail

    def test_g6_unreachable_timeout(self) -> None:
        """Timeout should be rejected with UNREACHABLE."""
        fetch_result = MockFetchResult(status="TIMEOUT", text=None, error="Request timeout")
        result = check_g6_reachability(fetch_result)
        assert result.allowed is False
        assert result.reason == "UNREACHABLE"

    def test_g6_unreachable_ssl_error(self) -> None:
        """SSL error should be rejected with UNREACHABLE."""
        fetch_result = MockFetchResult(status="SSL_ERROR", text=None, error="SSL handshake failed")
        result = check_g6_reachability(fetch_result)
        assert result.allowed is False
        assert result.reason == "UNREACHABLE"

    def test_g6_unreachable_text_none(self) -> None:
        """Successful status but text=None should be rejected."""
        fetch_result = MockFetchResult(status="OK", text=None)
        result = check_g6_reachability(fetch_result)
        assert result.allowed is False
        assert result.reason == "UNREACHABLE"

    def test_g6_missing_status(self) -> None:
        """Missing status attribute should be rejected."""
        fetch_result = MockFetchResult(status=None, text="text")
        result = check_g6_reachability(fetch_result)
        assert result.allowed is False
        assert result.reason == "UNREACHABLE"
