"""Entity Extraction Agent (A3): entities, kinds and verbatim quotes for ONE page chunk. Proposes only."""

from __future__ import annotations

from corpdisc.agents.base import BaseAgent
from corpdisc.agents.prompts import EXTRACTION_V1
from corpdisc.contracts.agent_io import ExtractionInput, ExtractionOutput
from corpdisc.contracts.agents import AgentError, AgentName, AgentRequest


class ExtractionAgent(BaseAgent[ExtractionInput, ExtractionOutput]):
    name = AgentName.EXTRACTION
    schema_version = "1"
    prompt_version = "1"
    input_model = ExtractionInput
    output_model = ExtractionOutput
    max_output_tokens = 3000

    def system_prompt(self) -> str:
        return EXTRACTION_V1

    def on_failure(self, request: AgentRequest[ExtractionInput], error: AgentError) -> ExtractionOutput | None:
        return None  # a failed chunk creates no entities; the node records EXTRACTION_FAILED (20.3)
