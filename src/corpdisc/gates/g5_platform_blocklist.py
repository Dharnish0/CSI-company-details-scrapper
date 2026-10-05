"""G5 — Platform Blocklist: reject social networks, code hosts, directories, site builders (ARCHITECTURE.md 22.8).

Reject:
- social networks (linkedin, facebook, twitter, instagram, youtube)
- code hosts (github, gitlab)
- directories (crunchbase, wikipedia)
- site builders (wixsite, squarespace, blogspot, github.io, sites.google)

Uses config.shared_platform_domains by default.
"""

from __future__ import annotations

from corpdisc.gates import GateResult
from corpdisc.services.normalize import registrable_domain

name: str = "G5_PLATFORM_BLOCKLIST"
description: str = "Reject social networks, code hosts, directories, site builders and shared platforms."


def check_g5_platform_blocklist(
    url: str,
    blocklist_domains: frozenset[str] | None = None,
) -> GateResult:
    """Reject blocklisted platforms.

    Args:
        url: The URL to check.
        blocklist_domains: Optional custom blocklist of shared platform domains.
            If None, uses DEFAULT_SHARED_PLATFORM_DOMAINS.

    Returns:
        GateResult with allowed=True if domain is not blocklisted, or
        allowed=False with reason="BLOCKED_PLATFORM" if it is.
    """
    # Default blocklist from config (same as in services/urls.py and config.py)
    default_blocklist = frozenset(
        {
            "linkedin.com",
            "facebook.com",
            "twitter.com",
            "x.com",
            "instagram.com",
            "youtube.com",
            "github.com",
            "medium.com",
            "wikipedia.org",
            "crunchbase.com",
            "wordpress.com",
            "blogspot.com",
            "wixsite.com",
            "squarespace.com",
            "github.io",
            "sites.google.com",
        }
    )

    effective_blocklist = blocklist_domains if blocklist_domains is not None else default_blocklist

    # Get registrable domain
    domain = registrable_domain(url)
    if domain is None:
        # Cannot determine domain, allow (will be caught elsewhere)
        return GateResult(True)

    domain_lower = domain.lower()

    # Check if domain or any parent is in blocklist
    for blocked in effective_blocklist:
        blocked_lower = blocked.lower()
        if domain_lower == blocked_lower or domain_lower.endswith("." + blocked_lower):
            return GateResult(
                False,
                "BLOCKED_PLATFORM",
                f"Domain {domain!r} is on the blocklist ({blocked})",
            )

    return GateResult(True)
