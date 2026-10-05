"""G11 — Evidence Requirement: CONFIRMED requires verified ownership evidence (ARCHITECTURE.md 22.14).

CONFIRMED requires VerifiedEvidence with:
- quote_verified = True
- supports = OWNERSHIP
- source_class in: FIRST_PARTY_PARENT, FIRST_PARTY_CHILD, OFFICIAL_DOCUMENT

Otherwise downgrade to REVIEW_REQUIRED.

Uses corpdisc.contracts.common.VerifiedEvidence.
"""

from __future__ import annotations

from corpdisc.contracts.common import VerifiedEvidence
from corpdisc.gates import GateResult
from corpdisc.models.domain import SourceClass, Supports, VerificationStatus

name: str = "G11_EVIDENCE_REQUIREMENT"
description: str = "CONFIRMED requires VerifiedEvidence with verified ownership evidence; otherwise downgrade to REVIEW_REQUIRED."


def check_g11_evidence_requirement(
    evidence: tuple[VerifiedEvidence, ...],
    require_for_confirmed: bool = True,
) -> GateResult:
    """Check evidence requirement for CONFIRMED status.

    Args:
        evidence: Tuple of VerifiedEvidence objects.
        require_for_confirmed: If True, CONFIRMED requires satisfying evidence.
            If False, only validate evidence structure.

    Returns:
        GateResult with allowed=True if evidence requirements are met (or not required),
        or allowed=False indicating that CONFIRMED is not supported by evidence.
        Note: This gate does NOT downgrade status; it only validates that CONFIRMED
        is supported. Downgrading to REVIEW_REQUIRED is done by the caller.
    """
    # If evidence is empty and not required, allow
    if not evidence and not require_for_confirmed:
        return GateResult(True)

    # Check each piece of evidence
    confirming_classes = frozenset({
        SourceClass.FIRST_PARTY_PARENT,
        SourceClass.FIRST_PARTY_CHILD,
        SourceClass.OFFICIAL_DOCUMENT,
    })

    has_satisfying_evidence = False

    for ev in evidence:
        # Must have quote_verified=True
        if not ev.quote_verified:
            continue

        # Must support OWNERSHIP
        if ev.supports != Supports.OWNERSHIP:
            continue

        # Must be from a confirming source class
        if ev.source_class not in confirming_classes:
            continue

        has_satisfying_evidence = True
        break

    if require_for_confirmed and not has_satisfying_evidence:
        return GateResult(
            False,
            "INSUFFICIENT_EVIDENCE",
            "CONFIRMED requires verified ownership evidence from FIRST_PARTY_PARENT, FIRST_PARTY_CHILD, or OFFICIAL_DOCUMENT",
        )

    return GateResult(True)
