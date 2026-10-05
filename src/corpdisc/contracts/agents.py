"""Generic agent-facing contracts: AgentSpec, FailurePolicy, the request/response envelope
(ARCHITECTURE 22.1) and the audit records written by db/repos/audit.py.

Per-agent input/output payload schemas (22.4 to 22.10) live in contracts/agent_io.py.
"""

from __future__ import annotations

import json as _json
from datetime import datetime
from enum import StrEnum
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field, model_validator

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


# ---------------------------------------------------------------- M2: envelope (ARCHITECTURE 22.1)

class CallStatus(StrEnum):
    OK = "OK"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class FailureKind(StrEnum):
    TRANSPORT = "TRANSPORT"
    SCHEMA = "SCHEMA"
    GROUNDING = "GROUNDING"
    BUDGET = "BUDGET"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    TOOL = "TOOL"


class Meta(Strict):
    run_id: int
    task_id: int | None
    correlation_id: str = Field(min_length=1, max_length=200)  # "run:task:stage"
    agent: AgentName
    schema_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    attempt: int = Field(default=1, ge=1)


class CallLimits(Strict):
    max_output_tokens: int = Field(ge=1)
    max_tool_calls: int = Field(default=0, ge=0)


class Usage(Strict):
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    cache_hit: bool = False
    tool_calls: int = 0


class AgentError(Strict):
    kind: FailureKind
    message: str = Field(max_length=500)


In = TypeVar("In", bound=BaseModel)
Out = TypeVar("Out", bound=BaseModel)


class AgentRequest(Strict, Generic[In]):
    meta: Meta
    limits: CallLimits
    payload: In


class AgentResponse(Strict, Generic[Out]):
    meta: Meta
    status: CallStatus
    payload: Out | None = None
    warnings: tuple[str, ...] = ()
    usage: Usage = Usage()
    error: AgentError | None = None

    @model_validator(mode="after")
    def _status_rules(self) -> AgentResponse[Out]:
        if self.status in (CallStatus.OK, CallStatus.PARTIAL) and self.payload is None:
            raise ValueError(f"{self.status} needs a payload")
        if self.status in (CallStatus.FAILED, CallStatus.SKIPPED) and (self.payload is not None or self.error is None):
            raise ValueError(f"{self.status} needs error and no payload")
        return self


# ---------------------------------------------------------------- M2: audit records (db/repos/audit.py)
class InvocationStart(Strict):
    run_id: int
    task_id: int | None
    agent: AgentName
    correlation_id: str
    schema_version: str
    prompt_version: str
    attempt: int = Field(ge=1)
    input_hash: str
    request_json: str
    started_at: datetime


class InvocationFinish(Strict):
    status: CallStatus
    failure_kind: FailureKind | None = None
    response_json: str | None = None
    cache_hit: bool = False
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    finished_at: datetime


class ToolCallRecord(Strict):
    invocation_id: int
    tool: str
    args_json: str
    allowed: bool
    result_summary: str | None = None
    duration_ms: int | None = None
    created_at: datetime


class CachedResponse(Strict):
    invocation_id: int
    response_json: str
    tokens_in: int | None = None
    tokens_out: int | None = None

    def payload(self) -> Any:
        return _json.loads(self.response_json).get("payload")
