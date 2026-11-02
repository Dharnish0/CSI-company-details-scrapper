"""Shared pieces of the real provider adapters: error classification, redaction, JSON hygiene."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from corpdisc.agents.ports import Message, ProviderAuthError, ProviderError, ProviderTransportError
from corpdisc.transport import HttpReply, TransportFailure

JSON_ONLY = (
    "\n\nOUTPUT FORMAT: reply with exactly one JSON object and nothing else (no prose, no markdown fences) that "
    "validates against this JSON Schema:\n"
)
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S | re.I)


def redact(text: str, *secrets: str | None) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "[REDACTED]")
    return re.sub(r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+\S+)", "[REDACTED]", text)


def retry_after(headers: Mapping[str, str]) -> float | None:
    v = headers.get("retry-after")
    try:
        return max(0.0, float(v)) if v is not None else None
    except ValueError:
        return _to_seconds(v)


def compact_schema(schema: Any) -> Any:
    """Drop noise keys (title/default/additionalProperties) from a JSON Schema to keep prompts short."""
    if isinstance(schema, dict):
        return {k: compact_schema(v) for k, v in schema.items() if k not in ("title", "default", "additionalProperties")}
    if isinstance(schema, list):
        return [compact_schema(v) for v in schema]
    return schema


def _to_seconds(value: str | None) -> float | None:
    """Parse '2m59.56s', '7.66s', '120ms', '30' (Groq/OpenAI style durations) into seconds."""
    if not value:
        return None
    m = re.fullmatch(r"\s*(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?\s*", value)
    if m and any(m.groups()):
        h, mi, s, ms = (float(g) if g else 0.0 for g in m.groups())
        return h * 3600 + mi * 60 + s + ms / 1000
    try:
        return float(value)
    except ValueError:
        return None


def ratelimit_metrics(headers: Mapping[str, str]) -> dict[str, Any]:
    """Extract x-ratelimit-* hints (remaining requests/tokens and reset delays) from response headers."""
    out: dict[str, Any] = {}
    for key, name in (("x-ratelimit-remaining-requests", "rl_remaining_requests"),
                      ("x-ratelimit-remaining-tokens", "rl_remaining_tokens"),
                      ("x-ratelimit-limit-requests", "rl_limit_requests"),
                      ("x-ratelimit-limit-tokens", "rl_limit_tokens")):
        try:
            if headers.get(key) is not None:
                out[name] = int(float(headers[key]))
        except ValueError:
            pass
    for key, name in (("x-ratelimit-reset-requests", "rl_reset_requests_s"), ("x-ratelimit-reset-tokens", "rl_reset_tokens_s")):
        v = _to_seconds(headers.get(key))
        if v is not None:
            out[name] = v
    return out


def strip_fences(text: str) -> str:
    m = _FENCE.match(text or "")
    return m.group(1) if m else (text or "").strip()


def failure_to_error(exc: TransportFailure) -> ProviderError:
    # TLS failures are configuration problems: never retried and never bypassed (25.1). A TIMEOUT is not retried either:
    # re-sending an identical request that already consumed the whole timeout only multiplies the wait (measured: 4 attempts
    # x 300 s per chunk). Connection errors (nothing was processed) are still retried with backoff.
    return ProviderTransportError(f"transport {exc.code}", retryable=exc.code == "CONNECTION")


def classify(reply: HttpReply, *, provider: str, secret: str | None) -> ProviderError | None:
    """Map an HTTP reply to a ProviderError (None = success). Messages are short and redacted."""
    s = reply.status
    if 200 <= s < 300:
        return None
    detail = ""
    try:
        err = json.loads(reply.body).get("error")
        detail = err.get("message", "") if isinstance(err, dict) else str(err or "")
    except (ValueError, AttributeError):
        pass
    detail = redact(detail, secret)[:200]
    if s in (401, 402, 403):  # bad key, no credit or quota, forbidden: never retried, opens the breaker
        return ProviderAuthError(f"{provider} auth/quota error {s}: {detail}")
    if s == 429 or s == 408 or s >= 500 or s == 529:
        return ProviderTransportError(f"{provider} transient error {s}", retryable=True, retry_after_s=retry_after(reply.headers),
                                       rate_limited=s == 429, detail=detail, rl_metrics=ratelimit_metrics(reply.headers))
    return ProviderTransportError(f"{provider} rejected the request ({s}): {detail}", retryable=False)


def parse_json_body(reply: HttpReply, provider: str) -> dict[str, Any]:
    try:
        data = json.loads(reply.body)
    except ValueError as exc:
        raise ProviderTransportError(f"{provider} returned a non-JSON body", retryable=False) from exc
    if not isinstance(data, dict):
        raise ProviderTransportError(f"{provider} returned an unexpected body", retryable=False)
    return data


def merge_messages(messages: Sequence[Message]) -> list[dict[str, str]]:
    """Map our roles to user/assistant and merge adjacent same-role turns (tool results are DATA, sent as user)."""
    out: list[dict[str, str]] = []
    for m in messages:
        role = "assistant" if m.role == "assistant" else "user"
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + m.content
        else:
            out.append({"role": role, "content": m.content})
    return out
