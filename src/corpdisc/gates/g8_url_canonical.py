"""G8 — URL Canonical: use existing canonicalization; reject duplicate URLs (ARCHITECTURE.md 22.11).

Use existing URL canonicalization. Reject URLs already visited for the entity.
Uses corpdisc.services.urls.canonicalize_url and tracks visited URLs.
"""

from __future__ import annotations

from corpdisc.gates import GateResult
from corpdisc.services.urls import canonicalize_url

name: str = "G8_URL_CANONICAL"
description: str = "Use existing URL canonicalization; reject already-visited URLs for the entity."


def check_g8_url_canonical(
    url: str,
    base_url: str | None,
    canonical_store: set[str],
    entity_id: int | None = None,
) -> GateResult:
    """Use existing canonicalization; reject duplicate URLs.

    Args:
        url: The URL to canonicalize and check.
        base_url: Optional base URL for resolving relative URLs.
        canonical_store: Set of already-visited canonical URLs for this entity.
        entity_id: Optional entity ID (for context in error messages).

    Returns:
        GateResult with allowed=True if URL is new, or
        allowed=False with reason="DUPLICATE_URL" if already seen.
    """
    result = canonicalize_url(url, base=base_url)

    if not result.ok:
        return GateResult(
            False,
            "URL_INVALID",
            f"URL {url!r} could not be canonicalized: {result.detail}",
        )

    canonical_url = result.canonical

    if canonical_url in canonical_store:
        entity_context = f" for entity {entity_id}" if entity_id is not None else ""
        return GateResult(
            False,
            "DUPLICATE_URL",
            f"URL {url!r} (canonical: {canonical_url!r}) already visited{entity_context}",
        )

    return GateResult(True)
