"""G13 — Conservative Direction: never promote status or confidence (ARCHITECTURE.md 22.16).

Agents must never promote status or confidence. Demotion is allowed.

- CONFIRMED -> REVIEW_REQUIRED: OK (demotion)
- REVIEW_REQUIRED -> CONFIRMED: GateResult(False, "PROMOTION_PROHIBITED", ...)
- confidence increase: GateResult(False, "CONFIDENCE_PROMOTION_PROHIBITED", ...)

Uses corpdisc.models.domain.VerificationStatus.
"""

from __future__ import annotations

from corpdisc.gates import GateResult
from corpdisc.models.domain import VerificationStatus

name: str = "G13_CONSERVATIVE_DIRECTION"
description: str = "Agents must never promote status or confidence; demotion is allowed."


def check_g13_conservative_direction(
    status: VerificationStatus,
    confidence: float,
    previous_status: VerificationStatus,
    previous_confidence: float,
) -> GateResult:
    """Enforce conservative direction: no promotion of status or confidence.

    Args:
        status: The proposed new status.
        confidence: The proposed new confidence.
        previous_status: The previous status.
        previous_confidence: The previous confidence.

    Returns:
        GateResult with allowed=True if no promotion occurred, or
        allowed=False with reason="PROMOTION_PROHIBITED" or "CONFIDENCE_PROMOTION_PROHIBITED".
    """
    status_order = {
        VerificationStatus.CONFIRMED: 2,
        VerificationStatus.REVIEW_REQUIRED: 1,
        VerificationStatus.REJECTED: 0,
    }

    current_status_rank = status_order.get(status, 0)
    previous_status_rank = status_order.get(previous_status, 0)

    # Check status promotion
    if current_status_rank > previous_status_rank:
        return GateResult(
            False,
            "PROMOTION_PROHIBITED",
            f"Status cannot be promoted from {previous_status.value} to {status.value}",
        )

    # Check confidence increase
    if confidence > previous_confidence:
        return GateResult(
            False,
            "CONFIDENCE_PROMOTION_PROHIBITED",
            f"Confidence cannot be increased from {previous_confidence} to {confidence}",
        )

    return GateResult(True)
