"""Tests for G4 — Scope."""

from corpdisc.gates import check_g4_scope


class TestG4Scope:
    """Tests for G4 — Scope."""

    def test_g4_allowed_corporate_page(self) -> None:
        """Corporate pages should be allowed."""
        result = check_g4_scope("COMPANIES", "https://example.com/companies")
        assert result.allowed is True

    def test_g4_excluded_careers(self) -> None:
        """Careers pages should be rejected."""
        result = check_g4_scope("CAREERS", "https://example.com/careers")
        assert result.allowed is False
        assert result.reason == "EXCLUDED_SCOPE"
        assert "CAREERS" in result.detail

    def test_g4_excluded_blog(self) -> None:
        """Blog pages should be rejected."""
        result = check_g4_scope("BLOG", "https://example.com/blog")
        assert result.allowed is False
        assert result.reason == "EXCLUDED_SCOPE"

    def test_g4_excluded_news(self) -> None:
        """News pages should be rejected."""
        result = check_g4_scope("NEWS", "https://example.com/news")
        assert result.allowed is False
        assert result.reason == "EXCLUDED_SCOPE"

    def test_g4_excluded_partners(self) -> None:
        """Partners pages should be rejected."""
        result = check_g4_scope("PARTNERS", "https://example.com/partners")
        assert result.allowed is False
        assert result.reason == "EXCLUDED_SCOPE"

    def test_g4_excluded_customers(self) -> None:
        """Customers pages should be rejected."""
        result = check_g4_scope("CUSTOMERS", "https://example.com/customers")
        assert result.allowed is False
        assert result.reason == "EXCLUDED_SCOPE"

    def test_g4_excluded_case_studies(self) -> None:
        """Case studies pages should be rejected."""
        result = check_g4_scope("CASE_STUDIES", "https://example.com/case-studies")
        assert result.allowed is False
        assert result.reason == "EXCLUDED_SCOPE"

    def test_g4_guessed_careers_from_url(self) -> None:
        """URL with /careers/ path should be detected as careers."""
        result = check_g4_scope(None, "https://example.com/careers/software-engineer")
        assert result.allowed is False
        assert result.reason == "EXCLUDED_SCOPE"

    def test_g4_guessed_blog_from_url(self) -> None:
        """URL with /blog/ path should be detected as blog."""
        result = check_g4_scope(None, "https://example.com/blog/news")
        assert result.allowed is False
        assert result.reason == "EXCLUDED_SCOPE"

    def test_g4_unknown_type_allowed(self) -> None:
        """Unknown page types should be allowed (filtered later)."""
        result = check_g4_scope("UNKNOWN_TYPE", "https://example.com/something")
        assert result.allowed is True
