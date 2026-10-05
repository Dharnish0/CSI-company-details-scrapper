"""Deduplication Agent (A6): judges AMBIGUOUS pairs only; the deterministic ladder always runs first."""

from __future__ import annotations

from corpdisc.agents.base import BaseAgent
from corpdisc.agents.prompts import DEDUP_V1
from corpdisc.contracts.agent_io import DuplicateInput, DuplicateOutput, DuplicateVerdict
from corpdisc.contracts.agents import AgentError, AgentName, AgentRequest


class DedupAgent(BaseAgent[DuplicateInput, DuplicateOutput]):
    name = AgentName.DEDUP
    schema_version = "1"
    prompt_version = "1"
    input_model = DuplicateInput
    output_model = DuplicateOutput
    max_output_tokens = 1500

    def system_prompt(self) -> str:
        return DEDUP_V1

    def on_failure(self, request: AgentRequest[DuplicateInput], error: AgentError) -> DuplicateOutput:
        """Every pair is UNSURE: entities stay separate and get a review item (20.3)."""
        return DuplicateOutput(verdicts=[
            DuplicateVerdict(pair_id=p.pair_id, verdict="UNSURE", agent_confidence=0.0, reason="agent unavailable")
            for p in request.payload.pairs])
