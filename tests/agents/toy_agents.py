"""Test-only agents (they borrow real AgentName values because the DB CHECK restricts them)."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from corpdisc.agents.base import BaseAgent
from corpdisc.contracts.agents import AgentName


class EchoIn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str


class EchoOut(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    answer: str
    score: float


class ToyEchoAgent(BaseAgent[EchoIn, EchoOut]):
    name = AgentName.DISCOVERY
    schema_version = "t1"
    prompt_version = "t1"
    input_model = EchoIn
    output_model = EchoOut
    max_output_tokens = 200

    def system_prompt(self) -> str:
        return "Echo the text."

    def on_failure(self, request, error):  # type: ignore[no-untyped-def]
        return None


class LookupArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int


class ToyToolAgent(ToyEchoAgent):
    name = AgentName.WEBSITE
    allowed_tools = {"lookup": 2}
    max_tool_calls = 3
