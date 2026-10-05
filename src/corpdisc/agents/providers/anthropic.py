"""Anthropic Messages API adapter (no SDK, D16). Request/response shapes follow the public API documentation.

Structured output is requested by instruction plus the JSON Schema in the system prompt and verified by the
AgentRunner's Pydantic validation (schema repair applies). Tool use maps to ToolCallRequest; tool results are fed
back by the runner as user-role DATA. This adapter was written without live access: see tests/live for the opt-in
calibration that exercises it against the real API.
"""

from __future__ import annotations

import json

from corpdisc.agents.ports import ProviderRequest, ProviderResponse, ProviderTransportError, ProviderUsage, ToolCallRequest
from corpdisc.agents.providers.http_common import (
    JSON_ONLY,
    classify,
    failure_to_error,
    merge_messages,
    parse_json_body,
    strip_fences,
)
from corpdisc.transport import JsonTransport, TransportFailure


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, *, api_key: str, transport: JsonTransport, base_url: str = "", timeout: float = 60.0,
                 price_in_per_mtok: float = 0.0, price_out_per_mtok: float = 0.0, api_version: str = "2023-06-01") -> None:
        if not api_key:
            raise ValueError("an API key is required")
        self._key = api_key
        self._t = transport
        self._url = (base_url.rstrip("/") or "https://api.anthropic.com") + "/v1/messages"
        self._timeout = timeout
        self._pin, self._pout = price_in_per_mtok, price_out_per_mtok
        self._version = api_version

    def __repr__(self) -> str:  # never expose the key
        return "AnthropicProvider(key=***)"

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        body: dict = {
            "model": request.model, "max_tokens": request.max_output_tokens, "temperature": request.temperature,
            "system": request.system + JSON_ONLY + json.dumps(request.response_schema, separators=(",", ":")),
            "messages": merge_messages(request.messages),
        }
        if request.tools:
            body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.args_schema}
                             for t in request.tools]
        headers = {"x-api-key": self._key, "anthropic-version": self._version, "content-type": "application/json"}
        try:
            reply = self._t.post(self._url, headers=headers, body=json.dumps(body), timeout=self._timeout)
        except TransportFailure as exc:
            raise failure_to_error(exc) from None
        if (err := classify(reply, provider=self.name, secret=self._key)) is not None:
            raise err
        data = parse_json_body(reply, self.name)
        text, calls = [], []
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text.append(str(block.get("text", "")))
            elif block.get("type") == "tool_use":
                args = block.get("input")
                calls.append(ToolCallRequest(call_id=str(block.get("id", "")), name=str(block.get("name", "")),
                                             arguments=args if isinstance(args, dict) else {}))
        content = strip_fences("".join(text)) if text else None
        if not content and not calls:
            raise ProviderTransportError("anthropic returned no content", retryable=True)
        u = data.get("usage") or {}
        tin, tout = int(u.get("input_tokens", 0) or 0), int(u.get("output_tokens", 0) or 0)
        cost = (tin * self._pin + tout * self._pout) / 1_000_000
        return ProviderResponse(content=content or None, tool_calls=tuple(calls),
                                usage=ProviderUsage(tin, tout, round(cost, 6)), model=str(data.get("model") or request.model))
