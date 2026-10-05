"""G3 — Quote Grounding: quote must be verified using existing QuoteVerifier (ARCHITECTURE.md 22.6).

Quote must be verified (normalized exact match). Use the existing QuoteVerifier.
No fuzzy matching. Reject with reason "UNVERIFIED_QUOTE" if quote is not found.

See D5: normalized exact match only, NO fuzzy at 0.95 threshold.
"""

from __future__ import annotations

from corpdisc.gates import GateResult

name: str = "G3_QUOTE_GROUNDING"
description: str = "Quote must be verified by QuoteVerifier (normalized exact match, no fuzzy)."


def check_g3_quote_grounding(
    quote: str, page_text: str | None, verifier: object
) -> GateResult:
    """Quote must be verified (normalized exact match).

    Args:
        quote: The quote string to verify.
        page_text: The page text to search in.
        verifier: The QuoteVerifier instance (from corpdisc.services.quotes).

    Returns:
        GateResult with allowed=True if quote is verified, or
        allowed=False with reason="UNVERIFIED_QUOTE" if not found.
    """
    # QuoteVerifier.check returns QuoteCheck(verified, char_start, char_end, reason)
    result = verifier.check(quote, page_text or "")

    if not result.verified:
        return GateResult(False, "UNVERIFIED_QUOTE", f"Quote not found in page text: reason={result.reason}")

    return GateResult(True)
