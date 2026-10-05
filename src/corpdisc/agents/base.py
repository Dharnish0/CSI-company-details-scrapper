"""BaseAgent and small pure helpers (plan 4.M2.2).

An agent is a plain class: identity, versions, input/output models, prompt, tool allowlist and a
deterministic ``on_failure`` fallback. It has no handle to the DB, network or filesystem; the
AgentRunner drives it and graph nodes apply the fallback.
"""

from __future__ import annotations

import json
import math
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar, Generic

from corpdisc.contracts.agents import AgentError, AgentName, AgentRequest, In, Out


def make_correlation_id(run_id: int, task_id: int | None, stage: str) -> str:
    return f"{run_id}:{'-' if task_id is None else task_id}:{stage}"


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text or "") / 4)


class AgentContractError(RuntimeError):
    """Programmer errors only (wrong agent in meta, wrong payload type). Never raised for LLM output."""


class BaseAgent(ABC, Generic[In, Out]):
    name: ClassVar[AgentName]
    schema_version: ClassVar[str]
    prompt_version: ClassVar[str]
    input_model: ClassVar[type[Any]]
    output_model: ClassVar[type[Any]]
    allowed_tools: ClassVar[Mapping[str, int]] = {}  # tool name -> per-invocation cap
    max_output_tokens: ClassVar[int] = 1000
    max_tool_calls: ClassVar[int] = 0

    @abstractmethod
    def system_prompt(self) -> str: ...

    def user_message(self, payload: In) -> str:
        return canonical_json(payload.model_dump(mode="json"))

    @abstractmethod
    def on_failure(self, request: AgentRequest[In], error: AgentError) -> Out | None:
        """Safe deterministic fallback, applied by the CALLER (graph node), never by the runner."""
