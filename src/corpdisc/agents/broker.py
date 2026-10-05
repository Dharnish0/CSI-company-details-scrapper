"""ToolBroker (plan 4.M2.6): allowlist, per-tool and total caps, argument validation, audit.

Handlers are injected read-only closures; the broker never exposes a DB or HTTP handle. Every outcome,
allowed or denied, is written through the AuditSink and returned as a ToolResult (never raised).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from corpdisc.agents.base import BaseAgent, canonical_json
from corpdisc.agents.ports import AuditSink, BudgetPort, Clock, ToolCallRequest, ToolDescriptor
from corpdisc.contracts.agents import AgentName, ToolCallRecord


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[BaseModel], Mapping[str, Any]]
    read_only: bool = True
    max_result_chars: int = 4000


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: Mapping[str, Any] | None
    error: str | None
    truncated: bool = False

    def as_message(self) -> str:
        if self.ok:
            return canonical_json({"ok": True, "data": self.data, "truncated": self.truncated})
        return canonical_json({"ok": False, "error": self.error})


@dataclass
class ToolSession:
    agent: AgentName
    allowed: Mapping[str, int]
    max_total: int
    task_id: int | None = None
    counts: dict[str, int] = field(default_factory=dict)
    total: int = 0


class ToolBroker:
    def __init__(self, specs: Sequence[ToolSpec], *, audit: AuditSink, clock: Clock,
                 budget: BudgetPort | None = None) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for s in specs:
            if not s.read_only:
                raise ValueError(f"tool {s.name!r} must be read-only")
            if s.name in self._specs:
                raise ValueError(f"duplicate tool {s.name!r}")
            if s.args_model.model_config.get("extra") != "forbid":
                raise ValueError(f"tool {s.name!r} args_model must forbid extra fields")
            self._specs[s.name] = s
        self._audit = audit
        self._clock = clock
        self._budget = budget

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._specs)

    def descriptors_for(self, allowed: Mapping[str, int]) -> tuple[ToolDescriptor, ...]:
        return tuple(ToolDescriptor(s.name, s.description, s.args_model.model_json_schema())
                     for n, s in sorted(self._specs.items()) if n in allowed)

    def new_session(self, agent: BaseAgent, *, task_id: int | None = None) -> ToolSession:
        return ToolSession(agent=agent.name, allowed=dict(agent.allowed_tools), max_total=agent.max_tool_calls,
                           task_id=task_id)

    def _record(self, invocation_id: int, req: ToolCallRequest, allowed: bool, summary: str, t0: float) -> None:
        self._audit.record_tool_call(ToolCallRecord(
            invocation_id=invocation_id, tool=req.name[:100], args_json=canonical_json(req.arguments)[:4000],
            allowed=allowed, result_summary=summary[:500], duration_ms=int((self._clock.monotonic() - t0) * 1000),
            created_at=self._clock.now()))

    def call(self, session: ToolSession, invocation_id: int, request: ToolCallRequest) -> ToolResult:
        t0 = self._clock.monotonic()

        def deny(code: str, error: str) -> ToolResult:
            self._record(invocation_id, request, False, code, t0)
            return ToolResult(False, None, error)

        spec = self._specs.get(request.name)
        if spec is None or request.name not in session.allowed:
            return deny("DENIED:NOT_ALLOWLISTED", f"tool {request.name!r} is not allowed")
        if session.counts.get(request.name, 0) >= session.allowed[request.name] or session.total >= session.max_total:
            return deny("DENIED:CAP_EXCEEDED", f"tool call cap reached for {request.name!r}")
        if self._budget is not None:
            verdict = self._budget.check_tool_call(task_id=session.task_id)
            if not verdict.allow:
                return deny(f"DENIED:BUDGET:{verdict.reason}", "tool budget exhausted")
        session.counts[request.name] = session.counts.get(request.name, 0) + 1
        session.total += 1
        try:
            args = spec.args_model.model_validate(request.arguments)
        except ValidationError as exc:
            msg = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())[:300]
            self._record(invocation_id, request, True, f"ERROR:INVALID_ARGS: {msg}", t0)
            return ToolResult(False, None, f"invalid arguments: {msg}")
        try:
            data = dict(spec.handler(args))
            text = json.dumps(data, ensure_ascii=False, sort_keys=True)
        except Exception as exc:  # noqa: BLE001 - handler errors become tool errors, no traceback leaked
            self._record(invocation_id, request, True, f"ERROR:TOOL_ERROR:{type(exc).__name__}", t0)
            return ToolResult(False, None, f"tool error {type(exc).__name__}")
        truncated = len(text) > spec.max_result_chars
        if truncated:
            data = {"truncated_json": text[: spec.max_result_chars]}
        self._record(invocation_id, request, True, f"OK: {text[:480]}", t0)
        return ToolResult(True, data, None, truncated)
