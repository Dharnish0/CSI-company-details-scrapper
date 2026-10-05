"""G1 — ID Integrity: reject unknown runtime IDs (ARCHITECTURE.md 22.4).

Every referenced runtime ID must have been supplied. Unknown IDs are rejected with
reason "UNKNOWN_ID".
"""

from __future__ import annotations

from corpdisc.gates import GateResult

name: str = "G1_ID_INTEGRITY"
description: str = "Reject unknown runtime IDs that were not supplied by the runtime."


def check_g1_id_integrity(item_ref: str, known_ids: frozenset[int]) -> GateResult:
    """Reject unknown runtime IDs.

    Args:
        item_ref: The runtime ID to validate (as a string for JSON compatibility).
        known_ids: The set of IDs that were supplied by the runtime.

    Returns:
        GateResult with allowed=True if the ID is known, or allowed=False with
        reason="UNKNOWN_ID" if the ID is not in the known set.
    """
    try:
        id_value = int(item_ref)
    except (ValueError, TypeError):
        return GateResult(False, "UNKNOWN_ID", f"item_ref {item_ref!r} is not a valid integer ID")

    if id_value not in known_ids:
        return GateResult(False, "UNKNOWN_ID", f"item_ref {item_ref!r} not in known set")

    return GateResult(True)
