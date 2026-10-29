"""OpenAI provider using gpt-4.1-mini with structured output and budget guard.

Uses OpenAI's /v1/chat/completions endpoint with response_format.json_schema
for structured extraction. Verified downstream by Pydantic validation.

Pricing (USD per 1M tokens, as of 2025-06):
- Input: $0.40
- Cached input: $0.10
- Output: $1.60
- Max output tokens: 32,768
"""

from __future__ import annotations

import json

from corpdisc.agents.ports import ProviderRequest, ProviderResponse, ProviderTransportError, ProviderUsage, ToolCallRequest
from corpdisc.agents.providers.http_common import classify, failure_to_error, merge_messages, parse_json_body, strip_fences
from corpdisc.transport import JsonTransport, TransportFailure


class OpenAIProvider:
    """OpenAI provider with gpt-4.1-mini model and built-in pricing."""

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        transport: JsonTransport,
        base_url: str = "",
        timeout: float = 60.0,
        model_name: str = "gpt-4.1-mini",
    ) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required for openai provider")
        self._key = api_key
        self._t = transport
        self._url = (base_url.rstrip("/") or "https://api.openai.com") + "/v1/chat/completions"
        self._timeout = timeout
        # Official OpenAI pricing for gpt-4.1-mini (USD per million tokens)
        self._pin = 0.40  # Input per M token
        self._pout = 1.60  # Output per M token
        self._model_name = model_name.strip() or "gpt-4.1-mini"

    def __repr__(self) -> str:
        return f"OpenAIProvider(key=***, model={self._model_name!r})"

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Execute an OpenAI API call with structured output.

        Uses response_format.json_schema for structured extraction.
        Also supports function/tool calling via the tools parameter.
        """
        msgs = [{"role": "system", "content": request.system}, *merge_messages(request.messages)]

        body: dict[str, object] = {
            "model": self._model_name,
            "messages": msgs,
            "temperature": request.temperature,
            "max_completion_tokens": request.max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name[:60] or "output",
                    "schema": request.response_schema,
                    "strict": False,
                },
            },
        }

        # Add tools if provided (for tool calling support)
        if request.tools:
            body["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.args_schema}}
                for t in request.tools
            ]

        headers = {"content-type": "application/json", "authorization": f"Bearer {self._key}"}

        try:
            raw_body = json.dumps(body)
            reply = self._t.post(
                self._url,
                headers=headers,
                body=raw_body,
                timeout=self._timeout,
            )
        except TransportFailure as exc:
            raise failure_to_error(exc) from None

        if (err := classify(reply, provider=self.name, secret=self._key)) is not None:
            raise err

        data = parse_json_body(reply, self.name)
        choices = data.get("choices") or []
        msg = (choices[0].get("message") if choices and isinstance(choices[0], dict) else None) or {}

        # Extract tool calls if present
        calls: list[ToolCallRequest] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            calls.append(
                ToolCallRequest(
                    call_id=str(tc.get("id", "")),
                    name=str(fn.get("name", "")),
                    arguments=args if isinstance(args, dict) else {},
                )
            )

        # Extract content
        raw_content = msg.get("content")
        content = strip_fences(raw_content) if isinstance(raw_content, str) and raw_content.strip() else None

        # Validate we got content or tool calls
        if not content and not calls:
            raise ProviderTransportError("openai returned no content", retryable=True)

        # Extract usage
        usage_data = data.get("usage") or {}
        prompt_tokens = int(usage_data.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage_data.get("completion_tokens", 0) or 0)

        finish_reason = choices[0].get("finish_reason") if choices and isinstance(choices[0], dict) else None

        return ProviderResponse(
            content=content,
            tool_calls=tuple(calls),
            finish_reason=finish_reason,
            metrics={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "request_bytes": len(raw_body.encode("utf-8")),
            },
            usage=ProviderUsage(
                prompt_tokens,
                completion_tokens,
                round((prompt_tokens * self._pin + completion_tokens * self._pout) / 1_000_000, 6),
            ),
            model=str(data.get("model") or self._model_name),
        )
