"""Discovery Agent (A2): choose which links of one site likely list companies. Proposes only."""

from __future__ import annotations

from corpdisc.agents.base import BaseAgent
from corpdisc.agents.prompts import DISCOVERY_V1
from corpdisc.contracts.agent_io import DiscoveryInput, DiscoveryOutput
from corpdisc.contracts.agents import AgentError, AgentName, AgentRequest


class DiscoveryAgent(BaseAgent[DiscoveryInput, DiscoveryOutput]):
    name = AgentName.DISCOVERY
    schema_version = "1"
    prompt_version = "1"
    input_model = DiscoveryInput
    output_model = DiscoveryOutput
    max_output_tokens = 800

    def system_prompt(self) -> str:
        return DISCOVERY_V1

    def on_failure(self, request: AgentRequest[DiscoveryInput], error: AgentError) -> DiscoveryOutput | None:
        return None  # caller falls back to deterministic ranking (25.5)
