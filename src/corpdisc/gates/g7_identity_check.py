"""G7 — Identity Check: site text/title must contain entity name or known alias (ARCHITECTURE.md 22.10).

Site text/title must contain entity name or a supplied alias. NO external searching.
Uses normalize_text for comparison.
"""

from __future__ import annotations

from corpdisc.gates import GateResult
from corpdisc.services.normalize import normalize_text

name: str = "G7_IDENTITY_CHECK"
description: str = "Site text/title must contain entity name or known alias; no external search."


def check_g7_identity_check(
    entity_name: str,
    site_text: str | None,
    site_title: str | None,
    aliases: frozenset[str] | None = None,
) -> GateResult:
    """Site text/title must contain entity name or known alias.

    Args:
        entity_name: The entity name to search for.
        site_text: Optional site text to search.
        site_title: Optional site title to search.
        aliases: Optional set of known aliases for the entity.

    Returns:
        GateResult with allowed=True if entity name or alias is found, or
        allowed=False with reason="IDENTITY_MISMATCH" if not found in either source.
    """
    normalized_entity = normalize_text(entity_name)

    # Search in site_text
    if site_text is not None:
        normalized_text = normalize_text(site_text)
        if normalized_entity in normalized_text:
            return GateResult(True)

    # Search in site_title
    if site_title is not None:
        normalized_title = normalize_text(site_title)
        if normalized_entity in normalized_title:
            return GateResult(True)

    # Search in aliases - check if any alias appears in site_text or site_title
    if aliases is not None:
        for alias in aliases:
            normalized_alias = normalize_text(alias)
            # Check if alias is in site_text
            if site_text is not None and normalized_alias in normalize_text(site_text):
                return GateResult(True)
            # Check if alias is in site_title
            if site_title is not None and normalized_alias in normalize_text(site_title):
                return GateResult(True)

    # Entity name/alias not found in any source
    return GateResult(False, "IDENTITY_MISMATCH", f"Entity name {entity_name!r} not found in site text or title")
