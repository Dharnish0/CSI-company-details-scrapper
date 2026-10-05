"""FakeLLMProvider: deterministic scripted provider for tests and stub runs (plan 4.M2.4)."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel

from corpdisc.agents.ports import ProviderError, ProviderRequest, ProviderResponse, ProviderTransportError
from corpdisc.agents.ports import ProviderUsage, ToolCallRequest


@dataclass(frozen=True)
class ToolCalls:
    calls: tuple[ToolCallRequest, ...]


FakeStep = str | dict | BaseModel | ToolCalls | ProviderError | Callable[[ProviderRequest], "str | ProviderResponse"]


class FakeLLMProvider:
    name = "fake"

    def __init__(self, script: Sequence[FakeStep] = (), *, model: str = "fake-model",
                 usage: ProviderUsage = ProviderUsage(100, 50, 0.0)) -> None:
        self._script = list(script)
        self._model = model
        self._usage = usage
        self.requests: list[ProviderRequest] = []

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def push(self, *steps: FakeStep) -> None:
        self._script.extend(steps)

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        if not self._script:
            raise ProviderTransportError("fake script exhausted", retryable=False)
        step = self._script.pop(0)
        if callable(step) and not isinstance(step, (BaseModel, ProviderError)):
            step = step(request)
        if isinstance(step, ProviderError):
            raise step
        if isinstance(step, ProviderResponse):
            return step
        if isinstance(step, ToolCalls):
            return ProviderResponse(content=None, tool_calls=step.calls, usage=self._usage, model=self._model)
        if isinstance(step, BaseModel):
            content = step.model_dump_json()
        elif isinstance(step, dict):
            content = json.dumps(step)
        else:
            content = str(step)
        return ProviderResponse(content=content, usage=self._usage, model=self._model)


class HandlerProvider:
    """Deterministic provider driven by a pure function ``handler(request) -> str | dict | BaseModel``.

    Used for the M4 stub agents and fixtures: the real AgentRunner, schema validation and gates still run,
    only the model is replaced by fixture data. Never touches network or DB.
    """

    name = "handler"

    def __init__(self, handler: Callable[[ProviderRequest], "str | dict | BaseModel"], *, model: str = "stub-model",
                 usage: ProviderUsage = ProviderUsage(100, 50, 0.0)) -> None:
        self._handler = handler
        self._model = model
        self._usage = usage
        self.requests: list[ProviderRequest] = []

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        out = self._handler(request)
        if isinstance(out, ToolCalls):
            return ProviderResponse(content=None, tool_calls=out.calls, usage=self._usage, model=self._model)
        if isinstance(out, BaseModel):
            out = out.model_dump_json()
        elif isinstance(out, dict):
            out = json.dumps(out)
        return ProviderResponse(content=out, usage=self._usage, model=self._model)
