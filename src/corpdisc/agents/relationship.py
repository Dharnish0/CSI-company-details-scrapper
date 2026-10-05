"""Relationship Verification Agent (A4): does the text support parent -> child ownership? Never decides status."""

from __future__ import annotations

from corpdisc.agents.base import BaseAgent
from corpdisc.agents.prompts import RELATIONSHIP_V1
from corpdisc.contracts.agent_io import RelationshipAssessment, RelationshipInput, RelationshipOutput
from corpdisc.contracts.agents import AgentError, AgentName, AgentRequest
from corpdisc.models.domain import RelationshipType


class RelationshipAgent(BaseAgent[RelationshipInput, RelationshipOutput]):
    name = AgentName.RELATIONSHIP
    schema_version = "1"
    prompt_version = "1"
    input_model = RelationshipInput
    output_model = RelationshipOutput
    max_output_tokens = 3000
    allowed_tools = {"get_page_excerpt": 2}  # 20.3: read-only, cap 2 per invocation
    max_tool_calls = 2

    def system_prompt(self) -> str:
        return RELATIONSHIP_V1

    def on_failure(self, request: AgentRequest[RelationshipInput], error: AgentError) -> RelationshipOutput:
        """Every child becomes INSUFFICIENT/Unknown: nothing can be confirmed (20.3)."""
        return RelationshipOutput(assessments=[
            RelationshipAssessment(local_ref=c.local_ref, support="INSUFFICIENT",
                                   relationship_type=RelationshipType.UNKNOWN, agent_confidence=0.0,
                                   reason="verification unavailable")
            for c in request.payload.children
        ])
