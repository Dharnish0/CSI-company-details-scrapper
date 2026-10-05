"""G9 — Budget: enforce existing budget limits (ARCHITECTURE.md 22.12).

Enforce existing budget limits: depth, per-domain page, total page, token,
tool-call limits. Return BUDGET_STOPPED when exhausted.

Uses BudgetGuard.check_depth, check_page, check_llm, check_tool_call.
"""

from __future__ import annotations

from corpdisc.gates import GateResult

name: str = "G9_BUDGET"
description: str = "Enforce existing budget limits: depth, per-domain page, total page, token, tool-call limits."


def check_g9_budget(
    budget_guard: object,
    *,
    depth: int | None = None,
    domain: str | None = None,
    llm_tokens: int | None = None,
    tool_calls: int | None = None,
    task_id: int | None = None,
) -> GateResult:
    """Enforce existing budget limits.

    Args:
        budget_guard: The BudgetGuard instance (from corpdisc.services.budget).
        depth: Optional depth to check.
        domain: Optional domain for per-domain page check.
        llm_tokens: Optional LLM token count for check.
        tool_calls: Optional tool call count for check.
        task_id: Optional task ID for per-task checks.

    Returns:
        GateResult with allowed=True if within budget, or
        allowed=False with reason="BUDGET_STOPPED" if exhausted.
    """
    # Check depth
    if depth is not None:
        result = budget_guard.check_depth(depth)
        if not result.allow:
            return GateResult(False, "BUDGET_STOPPED", f"Depth budget exceeded: {result.detail}")

    # Check per-domain page budget
    if domain is not None:
        result = budget_guard.check_page(domain)
        if not result.allow:
            return GateResult(False, "BUDGET_STOPPED", f"Page budget exceeded for domain {domain!r}: {result.detail}")

    # Check LLM budget (if token count provided)
    if llm_tokens is not None:
        # Use check_llm with minimal estimates
        result = budget_guard.check_llm(
            task_id=task_id,
            requested_output_tokens=llm_tokens,
            estimated_input_tokens=llm_tokens // 4,  # rough estimate
        )
        if not result.allow:
            return GateResult(False, "BUDGET_STOPPED", f"LLM budget exceeded: {result.detail}")

    # Check tool call budget
    if tool_calls is not None:
        result = budget_guard.check_tool_call(task_id=task_id)
        if not result.allow:
            return GateResult(False, "BUDGET_STOPPED", f"Tool call budget exceeded: {result.detail}")

    return GateResult(True)
