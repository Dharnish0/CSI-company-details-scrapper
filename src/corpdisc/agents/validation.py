"""Validation Agent (A7): semantic review of suspects the deterministic engine flagged. Can only lower status."""

from __future__ import annotations

from corpdisc.agents.base import BaseAgent
from corpdisc.agents.prompts import VALIDATION_V1
from corpdisc.contracts.agent_io import ValidationInput, ValidationOutput
from corpdisc.contracts.agents import AgentError, AgentName, AgentRequest


class ValidationAgent(BaseAgent[ValidationInput, ValidationOutput]):
    name = AgentName.VALIDATION
    schema_version = "1"
    prompt_version = "1"
    input_model = ValidationInput
    output_model = ValidationOutput
    max_output_tokens = 2500

    def system_prompt(self) -> str:
        return VALIDATION_V1

    def on_failure(self, request: AgentRequest[ValidationInput], error: AgentError) -> ValidationOutput | None:
        return None  # deterministic findings stand; suspects are listed as WARN "not semantically reviewed"
