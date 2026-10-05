"""G14 — Website Requirement: CONFIRMED non-root requires resolved primary website (ARCHITECTURE.md 22.17).

If REQUIRE_WEBSITE_FOR_CONFIRMED (Settings) is True:
- CONFIRMED non-root entity requires resolved primary website.
- Otherwise downgrade to REVIEW_REQUIRED.

Uses corpdisc.config.Settings.require_website_for_confirmed.
"""

from __future__ import annotations

from corpdisc.gates import GateResult

name: str = "G14_WEBSITE_REQUIREMENT"
description: str = "If REQUIRE_WEBSITE_FOR_CONFIRMED, CONFIRMED non-root entity requires resolved primary website."


def check_g14_website_requirement(
    website_url: str | None,
    entity_type: str,
    is_root: bool,
    *,
    require_for_confirmed: bool,
) -> GateResult:
    """Check website requirement for CONFIRMED status.

    Args:
        website_url: The resolved primary website URL.
        entity_type: The entity type (e.g., "Company", "Subsidiary").
        is_root: Whether this is the root entity.
        require_for_confirmed: If True, CONFIRMED requires a website.

    Returns:
        GateResult with allowed=True if website requirement is met, or
        allowed=False indicating that CONFIRMED requires a website.
        Note: This gate does NOT downgrade status; it only validates that CONFIRMED
        is supported. Downgrading to REVIEW_REQUIRED is done by the caller.
    """
    if not require_for_confirmed:
        return GateResult(True)

    # Root entities don't need a website for CONFIRMED
    if is_root:
        return GateResult(True)

    # Non-root entities require a website
    if website_url is None or not website_url.strip():
        return GateResult(
            False,
            "MISSING_WEBSITE",
            "CONFIRMED non-root entity requires a resolved primary website",
        )

    return GateResult(True)
