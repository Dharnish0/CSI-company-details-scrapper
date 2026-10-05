"""G4 — Scope: reject excluded page scopes (ARCHITECTURE.md 22.7).

Reject excluded page scopes: partners, customers, case studies, careers, blog,
non-acquisition news. Use ARCHITECTURE.md 22.2 excluded types.

Excluded types: PARTNERS, CUSTOMERS, CASE_STUDIES, CAREERS, BLOG, NEWS (non-acquisition)
"""

from __future__ import annotations

from corpdisc.gates import GateResult
from corpdisc.models.domain import PageType
from corpdisc.services.urls import canonicalize_url

name: str = "G4_SCOPE"
description: str = "Reject excluded page scopes: partners, customers, case studies, careers, blog, non-acquisition news."


def _guess_page_type_from_url(url: str) -> str | None:
    """Guess page type from URL path keywords (deterministic, no network)."""
    result = canonicalize_url(url)
    if not result.ok:
        return None

    path = result.canonical.lower() if result.canonical else ""
    domain = result.domain.lower() if result.domain else ""

    # Check for partner/case study keywords
    if "partner" in path or "partners" in path:
        return PageType.PARTNERS.value
    if "customer" in path or "customers" in path:
        return PageType.CUSTOMERS.value
    if "case" in path and "study" in path:
        return PageType.CASE_STUDIES.value

    # Check for career keywords
    if "career" in path or "job" in path or "work" in path:
        return PageType.CAREERS.value

    # Check for blog/news keywords
    if "blog" in path or "news" in path:
        return PageType.NEWS.value

    # Check common blog domains
    if any(domain.endswith(d) for d in [".blogspot.com", ".wordpress.com"]):
        return PageType.BLOG.value

    return None


def check_g4_scope(page_type: str | None, page_url: str) -> GateResult:
    """Reject excluded page scopes.

    Args:
        page_type: The known page type (if available).
        page_url: The page URL (used to guess page type if page_type is None).

    Returns:
        GateResult with allowed=True if page is allowed, or
        allowed=False with reason="EXCLUDED_SCOPE" if excluded.
    """
    # Determine page type
    effective_page_type = page_type
    if effective_page_type is None:
        effective_page_type = _guess_page_type_from_url(page_url)

    if effective_page_type is None:
        return GateResult(True)  # Unknown type, allow (will be filtered later)

    # Excluded types per ARCHITECTURE.md 22.2
    excluded_types = {
        PageType.PARTNERS.value,
        PageType.CUSTOMERS.value,
        PageType.CASE_STUDIES.value,
        PageType.CAREERS.value,
        PageType.BLOG.value,
        PageType.NEWS.value,
    }

    if effective_page_type in excluded_types:
        return GateResult(
            False,
            "EXCLUDED_SCOPE",
            f"Page type {effective_page_type!r} is excluded (partners, customers, case studies, careers, blog, news)",
        )

    return GateResult(True)
