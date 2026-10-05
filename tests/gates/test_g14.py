"""Tests for G14 — Website Requirement."""

from corpdisc.gates import check_g14_website_requirement


class TestG14WebsiteRequirement:
    """Tests for G14 — Website Requirement."""

    def test_g14_website_present(self) -> None:
        """Website present when required should be allowed."""
        result = check_g14_website_requirement(
            website_url="https://example.com",
            entity_type="Company",
            is_root=False,
            require_for_confirmed=True,
        )
        assert result.allowed is True

    def test_g14_website_missing_for_confirmed(self) -> None:
        """Website missing for CONFIRMED non-root should be rejected."""
        result = check_g14_website_requirement(
            website_url=None,
            entity_type="Company",
            is_root=False,
            require_for_confirmed=True,
        )
        assert result.allowed is False
        assert result.reason == "MISSING_WEBSITE"

    def test_g14_root_entity_no_website_required(self) -> None:
        """Root entity doesn't need website."""
        result = check_g14_website_requirement(
            website_url=None,
            entity_type="Parent Company",
            is_root=True,
            require_for_confirmed=True,
        )
        assert result.allowed is True

    def test_g14_require_for_confirmed_false(self) -> None:
        """If require_for_confirmed=False, website not required."""
        result = check_g14_website_requirement(
            website_url=None,
            entity_type="Company",
            is_root=False,
            require_for_confirmed=False,
        )
        assert result.allowed is True

    def test_g14_empty_url_string_rejected(self) -> None:
        """Empty string URL should be treated as missing."""
        result = check_g14_website_requirement(
            website_url="",
            entity_type="Company",
            is_root=False,
            require_for_confirmed=True,
        )
        assert result.allowed is False
        assert result.reason == "MISSING_WEBSITE"
