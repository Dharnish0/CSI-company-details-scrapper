"""Deterministic gate layer (M3): G1–G14.

Each gate is a pure function that returns GateResult(allowed, reason, detail).
Gates may call corpdisc.services.* but must NOT import agents/ or db/ and must NOT
create new DB connections. They receive DB connections as arguments where needed.

Gates are deterministic: no LLM, no network calls, no browser installation.
"""

from __future__ import annotations

from dataclasses import dataclass

from corpdisc.contracts.common import VerifiedEvidence

__all__ = [
    "GateResult",
    "GateRejectionRepo",
    "check_g1_id_integrity",
    "check_g2_name_grounding",
    "check_g3_quote_grounding",
    "check_g4_scope",
    "check_g5_platform_blocklist",
    "check_g6_reachability",
    "check_g7_identity_check",
    "check_g8_url_canonical",
    "check_g9_budget",
    "check_g10_cycle",
    "check_g11_evidence_requirement",
    "check_g12_schema_enum",
    "check_g13_conservative_direction",
    "check_g14_website_requirement",
]


@dataclass(frozen=True)
class GateResult:
    """Deterministic gate outcome."""

    allowed: bool
    reason: str | None = None
    detail: str | None = None


# Import gate modules to register their check functions
from corpdisc.gates import (
    g1_id_integrity,
    g2_name_grounding,
    g3_quote_grounding,
    g4_scope,
    g5_platform_blocklist,
    g6_reachability,
    g7_identity_check,
    g8_url_canonical,
    g9_budget,
    g10_cycle,
    g11_evidence_requirement,
    g12_schema_enum,
    g13_conservative_direction,
    g14_website_requirement,
)

# Re-export gate functions and metadata
check_g1_id_integrity = g1_id_integrity.check_g1_id_integrity
G1_ID_INTEGRITY = g1_id_integrity.name
G1_ID_INTEGRITY_DESCRIPTION = g1_id_integrity.description

check_g2_name_grounding = g2_name_grounding.check_g2_name_grounding
G2_NAME_GROUNDING = g2_name_grounding.name
G2_NAME_GROUNDING_DESCRIPTION = g2_name_grounding.description

check_g3_quote_grounding = g3_quote_grounding.check_g3_quote_grounding
G3_QUOTE_GROUNDING = g3_quote_grounding.name
G3_QUOTE_GROUNDING_DESCRIPTION = g3_quote_grounding.description

check_g4_scope = g4_scope.check_g4_scope
G4_SCOPE = g4_scope.name
G4_SCOPE_DESCRIPTION = g4_scope.description

check_g5_platform_blocklist = g5_platform_blocklist.check_g5_platform_blocklist
G5_PLATFORM_BLOCKLIST = g5_platform_blocklist.name
G5_PLATFORM_BLOCKLIST_DESCRIPTION = g5_platform_blocklist.description

check_g6_reachability = g6_reachability.check_g6_reachability
G6_REACHABILITY = g6_reachability.name
G6_REACHABILITY_DESCRIPTION = g6_reachability.description

check_g7_identity_check = g7_identity_check.check_g7_identity_check
G7_IDENTITY_CHECK = g7_identity_check.name
G7_IDENTITY_CHECK_DESCRIPTION = g7_identity_check.description

check_g8_url_canonical = g8_url_canonical.check_g8_url_canonical
G8_URL_CANONICAL = g8_url_canonical.name
G8_URL_CANONICAL_DESCRIPTION = g8_url_canonical.description

check_g9_budget = g9_budget.check_g9_budget
G9_BUDGET = g9_budget.name
G9_BUDGET_DESCRIPTION = g9_budget.description

check_g10_cycle = g10_cycle.check_g10_cycle
G10_CYCLE = g10_cycle.name
G10_CYCLE_DESCRIPTION = g10_cycle.description

check_g11_evidence_requirement = g11_evidence_requirement.check_g11_evidence_requirement
G11_EVIDENCE_REQUIREMENT = g11_evidence_requirement.name
G11_EVIDENCE_REQUIREMENT_DESCRIPTION = g11_evidence_requirement.description

check_g12_schema_enum = g12_schema_enum.check_g12_schema_enum
G12_SCHEMA_ENUM = g12_schema_enum.name
G12_SCHEMA_ENUM_DESCRIPTION = g12_schema_enum.description

check_g13_conservative_direction = g13_conservative_direction.check_g13_conservative_direction
G13_CONSERVATIVE_DIRECTION = g13_conservative_direction.name
G13_CONSERVATIVE_DIRECTION_DESCRIPTION = g13_conservative_direction.description

check_g14_website_requirement = g14_website_requirement.check_g14_website_requirement
G14_WEBSITE_REQUIREMENT = g14_website_requirement.name
G14_WEBSITE_REQUIREMENT_DESCRIPTION = g14_website_requirement.description
