"""Protocols and small value types the agent layer depends on (plan 4.M2.2).

Implementations live outside agents/: SqliteAuditSink / SqliteResponseCache in db/repos/audit.py,
BudgetGuard in services/budget.py. Clock and Sleeper are re-exported from the neutral clock module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from corpdisc.clock import Clock, Sleeper
from corpdisc.contracts.agents import AgentName, CachedResponse, InvocationFinish, InvocationStart, ToolCallRecord
from corpdisc.contracts.common import BudgetDecision

__all__ = [
    "AuditSink", "BudgetPort", "Clock", "LLMProvider", "Message", "ProviderAuthError", "ProviderError",
    "ProviderNotImplementedError", "ProviderRequest", "ProviderResponse", "ProviderTransportError", "ProviderUsage",
    "ResponseCache", "Sleeper", "ToolCallRequest", "ToolDescriptor",
]  # fmt: skip


class ProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after_s = retry_after_s


class ProviderTransportError(ProviderError):
    """Timeouts, 429, 5xx, connection errors (retryable) or fail-closed misses (not retryable)."""


class ProviderAuthError(ProviderError):
    """401/403/quota: never retried, opens the circuit breaker immediately (25.4)."""


class ProviderNotImplementedError(ProviderError):
    """A provider name that has no implementation yet."""


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str


@dataclass(frozen=True)
class ToolDescriptor:
    name: str
    description: str
    args_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCallRequest:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ProviderUsage:
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class ProviderRequest:
    model: str
    system: str
    messages: tuple[Message, ...]
    response_schema: dict[str, Any]
    schema_name: str
    temperature: float
    max_output_tokens: int
    tools: tuple[ToolDescriptor, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderResponse:
    content: str | None
    tool_calls: tuple[ToolCallRequest, ...] = ()
    usage: ProviderUsage = ProviderUsage()
    model: str = ""


class LLMProvider(Protocol):
    name: str

    def complete(self, request: ProviderRequest) -> ProviderResponse: ...  # raises ProviderError only


class AuditSink(Protocol):
    def start_invocation(self, rec: InvocationStart) -> int: ...
    def finish_invocation(self, invocation_id: int, rec: InvocationFinish) -> None: ...
    def record_tool_call(self, rec: ToolCallRecord) -> int: ...


class ResponseCache(Protocol):
    def lookup(self, *, agent: AgentName, schema_version: str, prompt_version: str,
               input_hash: str) -> CachedResponse | None: ...


class BudgetPort(Protocol):
    """Structurally satisfied by services.budget.BudgetGuard."""

    def check_llm(self, *, task_id: int | None, requested_output_tokens: int = 0,
                  agent_output_cap: int | None = None, estimated_input_tokens: int = 0) -> BudgetDecision: ...

    def check_tool_call(self, *, task_id: int | None) -> BudgetDecision: ...

    def record_llm_usage(self, *, tokens_in: int, tokens_out: int, cost_usd: float = 0.0) -> None: ...
