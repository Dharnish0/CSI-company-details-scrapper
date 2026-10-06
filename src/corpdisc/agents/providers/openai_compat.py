"""OpenAI-compatible Chat Completions adapter (OpenAI and compatible endpoints; no SDK, D16).

Uses ``response_format: json_schema`` (non-strict, so our permissive-but-validated schemas are accepted) and
function tools. Verified downstream by Pydantic validation. Written without live access; see tests/live.
"""

from __future__ import annotations

import json

from corpdisc.agents.ports import ProviderRequest, ProviderResponse, ProviderTransportError, ProviderUsage, ToolCallRequest
from corpdisc.agents.providers.http_common import classify, failure_to_error, merge_messages, parse_json_body, strip_fences
from corpdisc.transport import JsonTransport, TransportFailure


class OpenAICompatProvider:
    name = "openai"

    def __init__(self, *, api_key: str, transport: JsonTransport, base_url: str = "", timeout: float = 60.0,
                 price_in_per_mtok: float = 0.0, price_out_per_mtok: float = 0.0) -> None:
        # API key is optional for OpenAI-compatible providers (e.g., local Ollama)
        self._key = api_key
        self._t = transport
        self._url = (base_url.rstrip("/") or "https://api.openai.com") + "/v1/chat/completions"
        self._timeout = timeout
        self._pin, self._pout = price_in_per_mtok, price_out_per_mtok

    def __repr__(self) -> str:
        return "OpenAICompatProvider(key=***)" if self._key else "OpenAICompatProvider(key=none)"

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        msgs = [{"role": "system", "content": request.system}, *merge_messages(request.messages)]
        body: dict = {
            "model": request.model, "messages": msgs, "temperature": request.temperature,
            "max_completion_tokens": request.max_output_tokens,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": request.schema_name[:60] or "output", "schema": request.response_schema, "strict": False}},
        }
        if request.tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                               "parameters": t.args_schema}} for t in request.tools]
        headers = {"content-type": "application/json"}
        if self._key:
            headers["authorization"] = f"Bearer {self._key}"
        try:
            reply = self._t.post(self._url, headers=headers, body=json.dumps(body), timeout=self._timeout)
        except TransportFailure as exc:
            raise failure_to_error(exc) from None
        if (err := classify(reply, provider=self.name, secret=self._key)) is not None:
            raise err
        data = parse_json_body(reply, self.name)
        choices = data.get("choices") or []
        msg = (choices[0].get("message") if choices and isinstance(choices[0], dict) else None) or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            calls.append(ToolCallRequest(call_id=str(tc.get("id", "")), name=str(fn.get("name", "")),
                                         arguments=args if isinstance(args, dict) else {}))
        raw = msg.get("content")
        content = strip_fences(raw) if isinstance(raw, str) and raw.strip() else None
        if not content and not calls:
            raise ProviderTransportError("openai returned no content", retryable=True)
        u = data.get("usage") or {}
        tin, tout = int(u.get("prompt_tokens", 0) or 0), int(u.get("completion_tokens", 0) or 0)
        return ProviderResponse(content=content, tool_calls=tuple(calls),
                                usage=ProviderUsage(tin, tout, round((tin * self._pin + tout * self._pout) / 1_000_000, 6)),
                                model=str(data.get("model") or request.model))
