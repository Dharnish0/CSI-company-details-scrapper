"""Supervisor Advisor (A1): answers one narrow grey-zone question. Advisory only."""

from __future__ import annotations

from corpdisc.agents.base import BaseAgent
from corpdisc.agents.prompts import ADVISOR_V1
from corpdisc.contracts.agent_io import RecursionInput, RecursionOutput
from corpdisc.contracts.agents import AgentError, AgentName, AgentRequest


class AdvisorAgent(BaseAgent[RecursionInput, RecursionOutput]):
    name = AgentName.ADVISOR
    schema_version = "1"
    prompt_version = "1"
    input_model = RecursionInput
    output_model = RecursionOutput
    max_output_tokens = 300

    def system_prompt(self) -> str:
        return ADVISOR_V1

    def on_failure(self, request: AgentRequest[RecursionInput], error: AgentError) -> RecursionOutput | None:
        return None  # the fixed default policy of 25.5 applies
