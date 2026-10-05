"""G2 — Name Grounding: entity name must appear in page text or DOM candidate (ARCHITECTURE.md 22.5).

Name must appear in page text or DOM candidate. Reject with reason "NOT_IN_TEXT" if
the normalized name is not found in either.
"""

from __future__ import annotations

from corpdisc.gates import GateResult
from corpdisc.services.normalize import normalize_text

name: str = "G2_NAME_GROUNDING"
description: str = "Entity name must appear in page text or DOM candidate; reject if absent."


def check_g2_name_grounding(
    name: str, page_text: str | None = None, dom_candidate: str | None = None
) -> GateResult:
    """Name must appear in page text or DOM candidate.

    Args:
        name: The entity name to search for.
        page_text: Optional full page text to search.
        dom_candidate: Optional DOM candidate string to search.

    Returns:
        GateResult with allowed=True if name is found in either source, or
        allowed=False with reason="NOT_IN_TEXT" if name is absent from both.
    """
    normalized_name = normalize_text(name)

    # Search in page_text if provided
    if page_text is not None:
        normalized_page = normalize_text(page_text)
        if normalized_name in normalized_page:
            return GateResult(True)

    # Search in dom_candidate if provided
    if dom_candidate is not None:
        normalized_dom = normalize_text(dom_candidate)
        if normalized_name in normalized_dom:
            return GateResult(True)

    # Name not found in either source
    return GateResult(False, "NOT_IN_TEXT", f"Entity name {name!r} not found in page text or DOM candidate")
