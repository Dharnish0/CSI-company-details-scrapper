"""Tests for G3 — Quote Grounding."""

from corpdisc.gates import check_g3_quote_grounding
from corpdisc.services.quotes import QuoteVerifier


class TestG3QuoteGrounding:
    """Tests for G3 — Quote Grounding."""

    def test_g3_valid_quote(self) -> None:
        """Verified quote should be allowed."""
        verifier = QuoteVerifier()
        quote = "owned by ACME Corp"
        page_text = "This company is owned by ACME Corp"
        result = check_g3_quote_grounding(quote, page_text, verifier)
        assert result.allowed is True

    def test_g3_altered_quote_rejected(self) -> None:
        """Altered/unverified quote should be rejected with UNVERIFIED_QUOTE."""
        verifier = QuoteVerifier()
        quote = "acme owns this company"  # Not in page text
        page_text = "ACME Corp acquired the company"
        result = check_g3_quote_grounding(quote, page_text, verifier)
        assert result.allowed is False
        assert result.reason == "UNVERIFIED_QUOTE"

    def test_g3_empty_quote(self) -> None:
        """Empty quote should be rejected."""
        verifier = QuoteVerifier()
        result = check_g3_quote_grounding("", "some text", verifier)
        assert result.allowed is False
        assert result.reason == "UNVERIFIED_QUOTE"

    def test_g3_empty_page_text(self) -> None:
        """Empty page text should be rejected."""
        verifier = QuoteVerifier()
        result = check_g3_quote_grounding("some quote", "", verifier)
        assert result.allowed is False
        assert result.reason == "UNVERIFIED_QUOTE"

    def test_g3_none_page_text(self) -> None:
        """None page text is treated as empty."""
        verifier = QuoteVerifier()
        result = check_g3_quote_grounding("some quote", None, verifier)
        assert result.allowed is False
        assert result.reason == "UNVERIFIED_QUOTE"

    def test_g3_normalized_exact_match(self) -> None:
        """Normalized exact match should work (no fuzzy)."""
        verifier = QuoteVerifier()
        # Quote and page text with same normalized form (case-sensitive)
        quote = "ACME Corp"
        page_text = "ACME Corp is a leader"
        result = check_g3_quote_grounding(quote, page_text, verifier)
        assert result.allowed is True

    def test_g3_fuzzy_not_allowed(self) -> None:
        """Fuzzy matching is not allowed per D5."""
        verifier = QuoteVerifier()
        quote = "acme acquires"
        page_text = "ACME Corp completed the acquisition"
        result = check_g3_quote_grounding(quote, page_text, verifier)
        # "acme acquires" != "acme corp completed the acquisition"
        assert result.allowed is False
