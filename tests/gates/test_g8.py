"""Tests for G8 — URL Canonical."""

from corpdisc.gates import check_g8_url_canonical


class TestG8UrlCanonical:
    """Tests for G8 — URL Canonical."""

    def test_g8_canonical_new_url(self) -> None:
        """New canonical URL should be allowed."""
        canonical_store: set[str] = set()
        result = check_g8_url_canonical(
            url="https://example.com/About/",
            base_url=None,
            canonical_store=canonical_store,
            entity_id=1,
        )
        assert result.allowed is True

    def test_g8_duplicate_visited_rejected(self) -> None:
        """Duplicate visited URL should be rejected with DUPLICATE_URL."""
        canonical_store = {"https://example.com/About"}  # Note: path case is preserved
        result = check_g8_url_canonical(
            url="https://example.com/About/",
            base_url=None,
            canonical_store=canonical_store,
            entity_id=1,
        )
        assert result.allowed is False
        assert result.reason == "DUPLICATE_URL"
        assert "example.com" in result.detail

    def test_g8_invalid_url_rejected(self) -> None:
        """Invalid URL should be rejected with URL_INVALID."""
        canonical_store: set[str] = set()
        result = check_g8_url_canonical(
            url="not-a-valid-url",
            base_url=None,
            canonical_store=canonical_store,
        )
        assert result.allowed is False
        assert result.reason == "URL_INVALID"

    def test_g8_relative_url(self) -> None:
        """Relative URL with base should work."""
        canonical_store: set[str] = set()
        result = check_g8_url_canonical(
            url="/about",
            base_url="https://example.com",
            canonical_store=canonical_store,
        )
        assert result.allowed is True

    def test_g8_case_insensitive_duplicate(self) -> None:
        """Duplicate with different case should be rejected."""
        # Host is lowercased, path preserves case - they're different URLs
        canonical_store = {"https://example.com/ABOUT"}  # same case as the URL
        result = check_g8_url_canonical(
            url="https://EXAMPLE.com/ABOUT/",
            base_url=None,
            canonical_store=canonical_store,
        )
        assert result.allowed is False
        assert result.reason == "DUPLICATE_URL"
