"""Tests for G5 — Platform Blocklist."""

from corpdisc.gates import check_g5_platform_blocklist


class TestG5PlatformBlocklist:
    """Tests for G5 — Platform Blocklist."""

    def test_g5_legitimate_domain(self) -> None:
        """Legitimate corporate domain should be allowed."""
        result = check_g5_platform_blocklist("https://constellationsoftware.com")
        assert result.allowed is True

    def test_g5_blocklisted_linkedin(self) -> None:
        """LinkedIn should be rejected."""
        result = check_g5_platform_blocklist("https://linkedin.com/company/test")
        assert result.allowed is False
        assert result.reason == "BLOCKED_PLATFORM"
        assert "linkedin.com" in result.detail

    def test_g5_blocklisted_facebook(self) -> None:
        """Facebook should be rejected."""
        result = check_g5_platform_blocklist("https://facebook.com/test")
        assert result.allowed is False
        assert result.reason == "BLOCKED_PLATFORM"

    def test_g5_blocklisted_github(self) -> None:
        """GitHub should be rejected."""
        result = check_g5_platform_blocklist("https://github.com/test/test")
        assert result.allowed is False
        assert result.reason == "BLOCKED_PLATFORM"

    def test_g5_blocklisted_crunchbase(self) -> None:
        """Crunchbase should be rejected."""
        result = check_g5_platform_blocklist("https://crunchbase.com/organization/test")
        assert result.allowed is False
        assert result.reason == "BLOCKED_PLATFORM"

    def test_g5_blocklisted_wixsite(self) -> None:
        """Wixsite should be rejected."""
        result = check_g5_platform_blocklist("https://test.wixsite.com/site")
        assert result.allowed is False
        assert result.reason == "BLOCKED_PLATFORM"

    def test_g5_blocklisted_squarespace(self) -> None:
        """Squarespace should be rejected."""
        result = check_g5_platform_blocklist("https://test.squarespace.com")
        assert result.allowed is False
        assert result.reason == "BLOCKED_PLATFORM"

    def test_g5_blocklisted_wordpress(self) -> None:
        """WordPress.com should be rejected."""
        result = check_g5_platform_blocklist("https://test.wordpress.com")
        assert result.allowed is False
        assert result.reason == "BLOCKED_PLATFORM"

    def test_g5_custom_blocklist(self) -> None:
        """Custom blocklist should work."""
        custom_blocklist = frozenset({"blocked.com"})
        result = check_g5_platform_blocklist("https://blocked.com/page", blocklist_domains=custom_blocklist)
        assert result.allowed is False
        assert result.reason == "BLOCKED_PLATFORM"
        assert "blocked.com" in result.detail
