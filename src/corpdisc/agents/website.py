"""Website Resolution Agent (A5): pick an official site from supplied links/hits only. Proposes only."""

from __future__ import annotations

from corpdisc.agents.base import BaseAgent
from corpdisc.agents.prompts import WEBSITE_V1
from corpdisc.contracts.agent_io import WebsiteInput, WebsiteOutput
from corpdisc.contracts.agents import AgentError, AgentName, AgentRequest


class WebsiteAgent(BaseAgent[WebsiteInput, WebsiteOutput]):
    name = AgentName.WEBSITE
    schema_version = "1"
    prompt_version = "1"
    input_model = WebsiteInput
    output_model = WebsiteOutput
    max_output_tokens = 600

    def system_prompt(self) -> str:
        return WEBSITE_V1

    def on_failure(self, request: AgentRequest[WebsiteInput], error: AgentError) -> WebsiteOutput:
        return WebsiteOutput(decision="NONE_FOUND", agent_confidence=0.0, reason="agent unavailable")
