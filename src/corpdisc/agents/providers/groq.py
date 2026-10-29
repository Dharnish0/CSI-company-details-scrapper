"""Groq provider using OpenAI-compatible API with structured output support.

Uses Groq's OpenAI-compatible /v1/chat/completions endpoint with response_format.json_schema
for structured extraction. Verified downstream by Pydantic validation.
"""

from __future__ import annotations

import json
from typing import Any

from corpdisc.agents.ports import ProviderRequest, ProviderResponse, ProviderTransportError, ProviderUsage, ToolCallRequest
from corpdisc.agents.providers.http_common import (
    JSON_ONLY,
    classify,
    compact_schema,
    failure_to_error,
    merge_messages,
    parse_json_body,
    ratelimit_metrics,
    strip_fences,
)
from corpdisc.logging_setup import get_logger
from corpdisc.transport import JsonTransport, TransportFailure


log = get_logger("agents.groq")
_NO_THINK = {"none", "off", "false", "0", "disabled"}


def _is_qwen3(model: str) -> bool:
    return "qwen3" in model.lower()


class GroqProvider:
    name = "groq"

    def __init__(
        self,
        *,
        api_key: str,
        transport: JsonTransport,
        base_url: str = "",
        timeout: float = 60.0,
        price_in_per_mtok: float = 0.0,
        price_out_per_mtok: float = 0.0,
        model_name: str = "llama-3.3-70b-versatile",
    ) -> None:
        if not api_key:
            raise ValueError("GROQ_API_KEY is required for groq provider")
        self._key = api_key
        self._t = transport
        self._url = (base_url.rstrip("/") or "https://api.groq.com/openai/v1") + "/chat/completions"
        self._timeout = timeout
        self._pin, self._pout = price_in_per_mtok, price_out_per_mtok
        self._model_name = model_name.strip() or "llama-3.3-70b-versatile"

    def __repr__(self) -> str:
        return "GroqProvider(key=***)"

    def _build_body(self, request: ProviderRequest, *, json_mode: str, reasoning: bool) -> dict[str, Any]:
        # The schema is ALWAYS in the system prompt: with tools Groq cannot enforce response_format, and without an
        # explicit schema the model invents field names (e.g. "results" instead of "assessments").
        system = request.system + JSON_ONLY + json.dumps(compact_schema(request.response_schema), separators=(",", ":"))
        msgs = [{"role": "system", "content": system}, *merge_messages(request.messages)]
        body: dict[str, Any] = {
            "model": self._model_name,
            "messages": msgs,
            "temperature": request.temperature,
            "max_completion_tokens": request.max_output_tokens,
        }
        if request.tools:
            # Groq rejects/ignores structured outputs combined with tool use: omit response_format here.
            body["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.args_schema}}
                for t in request.tools
            ]
        elif json_mode == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": request.schema_name[:60] or "output", "schema": request.response_schema, "strict": False},
            }
        elif json_mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        if reasoning and request.reasoning is not None and request.reasoning.strip().lower() in _NO_THINK and _is_qwen3(self._model_name):
            body["reasoning_effort"] = "none"  # documented for Qwen3 models; other models reject it
        return body

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Execute a Groq chat completion.

        Without tools: ``response_format.json_schema`` (a 400 on it falls back once to ``json_object``).
        With tools: ``tools`` and no ``response_format``; the schema is in the system prompt and Pydantic validation
        plus the runner's one-shot repair enforce it.
        """
        headers = {"content-type": "application/json", "authorization": f"Bearer {self._key}"}
        json_mode, reasoning = "json_schema", True
        for _ in range(3):
            body = self._build_body(request, json_mode=json_mode, reasoning=reasoning)
            try:
                raw_body = json.dumps(body)
                reply = self._t.post(self._url, headers=headers, body=raw_body, timeout=self._timeout)
            except TransportFailure as exc:
                raise failure_to_error(exc) from None
            if reply.status == 400:
                text = reply.body.lower()
                if "reasoning_effort" in body and "reasoning" in text:
                    log.warning("groq rejected reasoning_effort for %s; retrying without it", self._model_name)
                    reasoning = False
                    continue
                if "response_format" in body and body["response_format"]["type"] == "json_schema" and (
                    "json_schema" in text or "response_format" in text or "structured" in text
                ):
                    log.warning("groq model %s rejected json_schema; falling back to json_object", self._model_name)
                    json_mode = "json_object"
                    continue
            break

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
            raise ProviderTransportError("groq returned no content", retryable=True)

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
                **ratelimit_metrics(reply.headers),
            },
            usage=ProviderUsage(
                prompt_tokens,
                completion_tokens,
                round((prompt_tokens * self._pin + completion_tokens * self._pout) / 1_000_000, 6),
            ),
            model=str(data.get("model") or self._model_name),
        )