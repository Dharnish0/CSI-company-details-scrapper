"""Generic agent-facing contract pieces: AgentSpec and FailurePolicy only.

Per-agent input/output payload schemas (ARCHITECTURE 22.4 to 22.10) belong to the
business-agent milestones and are intentionally absent.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field

from corpdisc.contracts.common import Strict


class AgentName(StrEnum):
    """Exactly the values allowed by the agent_invocations.agent CHECK constraint."""

    ADVISOR = "supervisor_advisor"
    DISCOVERY = "discovery"
    EXTRACTION = "extraction"
    RELATIONSHIP = "relationship_verification"
    WEBSITE = "website_resolution"
    DEDUP = "deduplication"
    VALIDATION = "validation"


class FailurePolicy(Strict):
    """What the runtime does when an agent call fails (deterministic default per ARCHITECTURE 25.5)."""

    on_failure: Literal["skip", "review", "fail_task"] = "review"
    max_schema_repairs: int = Field(default=1, ge=0, le=1)
    max_transport_retries: int = Field(default=2, ge=0, le=10)


class AgentSpec(Strict):
    """Static description of an agent: identity, versions, allowed tools and call caps."""

    name: AgentName
    schema_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    max_output_tokens: int = Field(default=1000, ge=1)
    tool_allowlist: tuple[str, ...] = ()
    max_tool_calls: int = Field(default=0, ge=0)
    failure_policy: FailurePolicy = FailurePolicy()
