"""G6 — Reachability: evaluate deterministic fetch results (ARCHITECTURE.md 22.9).

Evaluate supplied deterministic fetch results; NO network calls inside the gate.
Accept if fetch result status is OK or CACHED and text is not None.
Reject with "UNREACHABLE" for HTTP errors, timeouts, SSL errors, etc.
"""

from __future__ import annotations

from corpdisc.gates import GateResult

name: str = "G6_REACHABILITY"
description: str = "Evaluate deterministic fetch results; no network calls inside the gate."


def check_g6_reachability(fetch_result: object) -> GateResult:
    """Evaluate deterministic fetch results.

    Args:
        fetch_result: An object with attributes:
            - status: str ("OK", "CACHED", "HTTP_ERROR", "TIMEOUT", "SSL_ERROR", "CONNECTION_ERROR", etc.)
            - text: str | None (page text if successful)
            - error: str | None (error message if failed)

    Returns:
        GateResult with allowed=True if fetch was successful, or
        allowed=False with reason="UNREACHABLE" if it failed.
    """
    # Check status
    status = getattr(fetch_result, "status", None)
    text = getattr(fetch_result, "text", None)
    error = getattr(fetch_result, "error", None)

    # Determine if successful
    successful_statuses = {"OK", "CACHED"}

    if status is None:
        return GateResult(False, "UNREACHABLE", "Fetch result has no status attribute")

    if status not in successful_statuses:
        return GateResult(
            False,
            "UNREACHABLE",
            f"Fetch failed with status {status!r}: {error or 'unknown error'}",
        )

    if text is None:
        return GateResult(False, "UNREACHABLE", f"Fetch succeeded but text is None (status={status})")

    return GateResult(True)
