"""Tests for G7 — Identity Check."""

from corpdisc.gates import check_g7_identity_check


class TestG7IdentityCheck:
    """Tests for G7 — Identity Check."""

    def test_g7_matching_identity_in_text(self) -> None:
        """Matching entity name in site_text should be allowed."""
        result = check_g7_identity_check(
            entity_name="ACME Corp",
            site_text="ACME Corp is the parent company",
            site_title=None,
        )
        assert result.allowed is True

    def test_g7_matching_identity_in_title(self) -> None:
        """Matching entity name in site_title should be allowed."""
        result = check_g7_identity_check(
            entity_name="ACME Corp",
            site_text=None,
            site_title="ACME Corp - Home",
        )
        assert result.allowed is True

    def test_g7_unrelated_site_rejected(self) -> None:
        """Unrelated site should be rejected with IDENTITY_MISMATCH."""
        result = check_g7_identity_check(
            entity_name="ACME Corp",
            site_text="XYZ Corp homepage",
            site_title="XYZ",
        )
        assert result.allowed is False
        assert result.reason == "IDENTITY_MISMATCH"
        assert "ACME Corp" in result.detail

    def test_g7_matching_alias(self) -> None:
        """Matching entity alias should be allowed if alias is in site_text."""
        result = check_g7_identity_check(
            entity_name="ACME Corporation",
            site_text="ACME Corp operates here",
            site_title=None,
            aliases=frozenset({"ACME Corp"}),  # alias matches what's in site_text
        )
        # The alias "ACME Corp" is found in site_text "ACME Corp operates here"
        assert result.allowed is True

    def test_g7_normalized_match(self) -> None:
        """Normalization should allow case-insensitive match."""
        result = check_g7_identity_check(
            entity_name="acme corp",
            site_text="ACME Corp",
            site_title=None,
        )
        assert result.allowed is True

    def test_g7_both_sources_match(self) -> None:
        """Match in either source should be allowed."""
        result = check_g7_identity_check(
            entity_name="Test Corp",
            site_text="Test Corp",
            site_title="Different Name",
        )
        assert result.allowed is True

    def test_g7_empty_sources(self) -> None:
        """Empty sources should be rejected."""
        result = check_g7_identity_check(
            entity_name="Test Corp",
            site_text=None,
            site_title=None,
        )
        assert result.allowed is False
        assert result.reason == "IDENTITY_MISMATCH"
