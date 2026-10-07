"""Native Ollama adapter (``POST /api/chat``). No SDK (D16); the transport is injected.

Why native instead of the OpenAI-compatible shim: Ollama's /v1 layer reads ``max_tokens`` (not ``max_completion_tokens``),
cannot set ``num_ctx`` and offers only a coarse thinking switch, so a request through it was unbounded in output, default
thinking and a 4k context on CPU-only machines. The native API honours exactly what we need:

* ``options.num_predict``  - a real output cap (``done_reason == "length"`` when it is hit)
* ``options.num_ctx``      - an explicit context window (constant, so the model is not reloaded between requests)
* ``think``                - False for structured extraction (no hidden reasoning tokens eating the output cap)
* ``format``               - the JSON Schema as a grammar; the schema is ALSO put in the system prompt (Ollama's guidance
                             for grounding the model); when tools are offered ``format`` is omitted because a grammar
                             would forbid the tool call, and the runner's schema validation/repair covers the output.
* exact timings            - prompt_eval/eval/load/total durations and token counts for diagnostics.
"""

from __future__ import annotations

import json
from typing import Any

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

_NO_THINK = frozenset({"none", "off", "false", "disabled"})


def compact_schema(schema: Any) -> Any:
    """Prompt copy of a pydantic JSON Schema without noise keys (titles/defaults) - the grammar copy stays complete."""
    if isinstance(schema, dict):
        return {k: compact_schema(v) for k, v in schema.items() if k not in ("title", "default", "additionalProperties")}
    if isinstance(schema, list):
        return [compact_schema(v) for v in schema]
    return schema


class OllamaProvider:
    name = "ollama"

    def __init__(self, *, transport: JsonTransport, base_url: str = "", timeout: float = 300.0, num_ctx: int = 8192,
                 keep_alive: str = "30m") -> None:
        self._t = transport
        self._url = (base_url.rstrip("/") or "http://localhost:11434") + "/api/chat"
        self._timeout = timeout
        self._num_ctx = num_ctx
        self._keep_alive = keep_alive

    def __repr__(self) -> str:
        return f"OllamaProvider(url={self._url}, num_ctx={self._num_ctx})"

    def build_body(self, request: ProviderRequest) -> dict[str, Any]:
        system = request.system
        body: dict[str, Any] = {"model": request.model, "stream": False, "keep_alive": self._keep_alive,
                                "options": {"temperature": request.temperature, "num_predict": request.max_output_tokens,
                                            "num_ctx": self._num_ctx}}
        schema_text = json.dumps(compact_schema(request.response_schema), separators=(",", ":"))
        system += JSON_ONLY + schema_text
        if request.tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                               "parameters": t.args_schema}} for t in request.tools]
        else:
            body["format"] = request.response_schema  # grammar-constrained decoding
        if request.reasoning is not None:
            body["think"] = request.reasoning.strip().lower() not in _NO_THINK
        body["messages"] = [{"role": "system", "content": system}, *merge_messages(request.messages)]
        return body

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        body = self.build_body(request)
        raw = json.dumps(body)
        try:
            reply = self._t.post(self._url, headers={"content-type": "application/json"}, body=raw, timeout=self._timeout)
        except TransportFailure as exc:
            raise failure_to_error(exc) from None
        if (err := classify(reply, provider=self.name, secret=None)) is not None:
            raise err
        data = parse_json_body(reply, self.name)
        msg = data.get("message") or {}
        content = msg.get("content") if isinstance(msg.get("content"), str) else ""
        thinking = msg.get("thinking") if isinstance(msg.get("thinking"), str) else ""
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = (tc or {}).get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            calls.append(ToolCallRequest(call_id=str(tc.get("id") or f"call_{i}"), name=str(fn.get("name", "")),
                                         arguments=args if isinstance(args, dict) else {}))
        done_reason = data.get("done_reason") or ("stop" if data.get("done") else None)
        finish = "length" if done_reason == "length" else ("tool_calls" if calls else done_reason)
        content = strip_fences(content) if content.strip() else ""
        if not content and not calls and finish != "length":
            raise ProviderTransportError("ollama returned no content", retryable=True)
        pt, ct = int(data.get("prompt_eval_count") or 0), int(data.get("eval_count") or 0)
        ms = lambda k: round(int(data.get(k) or 0) / 1e6, 1)  # noqa: E731 - nanoseconds -> milliseconds
        metrics = {"request_bytes": len(raw.encode("utf-8")), "prompt_tokens": pt, "completion_tokens": ct,
                   "total_ms": ms("total_duration"), "load_ms": ms("load_duration"), "prompt_eval_ms": ms("prompt_eval_duration"),
                   "eval_ms": ms("eval_duration"), "reasoning_chars": len(thinking), "content_chars": len(content),
                   "num_ctx": self._num_ctx, "context_used_pct": round(100 * (pt + ct) / self._num_ctx, 1),
                   "think": body.get("think"), "num_predict": body["options"]["num_predict"]}
        return ProviderResponse(content=content or None, tool_calls=tuple(calls), usage=ProviderUsage(pt, ct, 0.0),
                                model=str(data.get("model") or request.model), finish_reason=finish, metrics=metrics)
