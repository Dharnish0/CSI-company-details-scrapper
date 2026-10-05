"""G10 — Cycle: use CycleGuard to reject cycles (ARCHITECTURE.md 22.13).

Use existing CycleGuard. Reject parent → child relationships that create a cycle
or violate task ancestry.

Uses CycleGuard.would_create_cycle(parent_id, child_id, ancestry=(), include_review=False).
"""

from __future__ import annotations

from corpdisc.gates import GateResult

name: str = "G10_CYCLE"
description: str = "Reject parent→child relationships that create a cycle or violate task ancestry."


def check_g10_cycle(
    cycle_guard: object,
    parent_id: int,
    child_id: int,
    ancestry: tuple[int, ...] = (),
) -> GateResult:
    """Reject cycles in parent→child relationships.

    Args:
        cycle_guard: The CycleGuard instance (from corpdisc.services.cycle_guard).
        parent_id: The parent entity ID.
        child_id: The child entity ID.
        ancestry: The task ancestry (tuple of parent IDs).

    Returns:
        GateResult with allowed=True if no cycle would be created, or
        allowed=False with reason="CYCLE" if adding this edge would create a cycle.
    """
    result = cycle_guard.would_create_cycle(parent_id, child_id, ancestry=ancestry)

    if result.creates_cycle:
        if result.reason:
            detail = result.reason
        else:
            detail = f"Adding {parent_id} → {child_id} would create a cycle"
        return GateResult(False, "CYCLE", detail)

    return GateResult(True)
