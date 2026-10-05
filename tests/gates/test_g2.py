"""Tests for G2 — Name Grounding."""

from corpdisc.gates import check_g2_name_grounding


class TestG2NameGrounding:
    """Tests for G2 — Name Grounding."""

    def test_g2_name_present_in_page_text(self) -> None:
        """Name found in page_text should be allowed."""
        result = check_g2_name_grounding("ACME Corp", page_text="ACME Corp is a leader in software")
        assert result.allowed is True

    def test_g2_name_present_in_dom_candidate(self) -> None:
        """Name found in dom_candidate should be allowed."""
        result = check_g2_name_grounding("Tech Solutions", dom_candidate="Tech Solutions Inc.")
        assert result.allowed is True

    def test_g2_name_absent_rejected(self) -> None:
        """Name not found should be rejected with NOT_IN_TEXT."""
        result = check_g2_name_grounding("NonExistent", page_text="ACME Corp homepage")
        assert result.allowed is False
        assert result.reason == "NOT_IN_TEXT"
        assert "NonExistent" in result.detail

    def test_g2_name_normalized_match(self) -> None:
        """Name should match with normalization (case-insensitive, whitespace)."""
        result = check_g2_name_grounding("  ACME  CORP  ", page_text="acme corp")
        assert result.allowed is True

    def test_g2_name_with_special_chars(self) -> None:
        """Name with special characters should normalize correctly."""
        # normalize_text drops non-word chars and casefolds
        result = check_g2_name_grounding("ACME's Corp", page_text="ACME's Corp")
        assert result.allowed is True

    def test_g2_empty_page_text(self) -> None:
        """Empty page_text with name in dom_candidate should work."""
        result = check_g2_name_grounding("Test Corp", page_text="", dom_candidate="Test Corp")
        assert result.allowed is True

    def test_g2_both_sources_empty(self) -> None:
        """Both sources empty should be rejected."""
        result = check_g2_name_grounding("Test Corp", page_text=None, dom_candidate=None)
        assert result.allowed is False
        assert result.reason == "NOT_IN_TEXT"
