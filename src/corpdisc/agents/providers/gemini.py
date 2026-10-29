"""Google Gemini adapter on the ``google-genai`` SDK, with native structured output.

Structured output is requested with ``response_json_schema`` (standard JSON Schema) and verified downstream by the
AgentRunner's Pydantic validation. Specific to Gemini 2.5/3 "thinking" models:

* Thinking tokens are billed as output and count toward ``max_output_tokens``. A structured extraction that thinks
  can spend the whole cap on thoughts and return truncated JSON (``finish_reason=MAX_TOKENS``). Thinking is therefore
  OFF for structured calls by default (``request.reasoning == "none"`` or ``GEMINI_THINKING=off``); with thinking on, the
  output cap is raised by an allowance so thoughts do not consume the answer budget.
* ``thinking_budget`` (Gemini 2.x) and ``thinking_level`` (Gemini 3) are mutually exclusive; the style is chosen from the
  model name. Thought parts are never mixed into the answer, and ``thoughts_token_count`` is added to ``tokens_out``.
"""

from __future__ import annotations

import re
from typing import Any

import google.genai as genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from corpdisc.agents.ports import (
    ProviderAuthError,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    ProviderTransportError,
    ProviderUsage,
)
from corpdisc.agents.providers.http_common import compact_schema, merge_messages
from corpdisc.logging_setup import get_logger
from corpdisc.transport import JsonTransport

log = get_logger("agents.gemini")

_NO_THINK = frozenset({"none", "off", "false", "disabled", "0"})
THINKING_MODES = ("off", "low", "default")
# Extra output tokens granted when the model is allowed to think (thoughts count against max_output_tokens).
_THINKING_ALLOWANCE = {"off": 0, "low": 1024, "default": 4096}
_LEVEL_MINIMAL_ALLOWANCE = 256  # Gemini 3 cannot fully disable thinking: "minimal" may still emit a few thought tokens
_FINISH_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "LANGUAGE": "content_filter",
}
_RETRY_DELAY = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s*$")


def uses_thinking_budget(model: str) -> bool:
    """Gemini 2.x takes ``thinking_budget`` (tokens); newer models take ``thinking_level``."""
    m = model.lower()
    return any(tag in m for tag in ("gemini-2.", "gemini-1."))


def finish_reason_name(value: Any) -> str | None:
    """Map the SDK finish reason (enum, ``FinishReason.STOP`` or ``FINISH_REASON_STOP``) to our normalised value."""
    if value is None:
        return None
    raw = getattr(value, "name", None) or str(value)
    raw = raw.split(".")[-1].upper().removeprefix("FINISH_REASON_")
    return _FINISH_MAP.get(raw, raw.lower())


def retry_delay_from_error(exc: genai_errors.APIError) -> float | None:
    """Parse ``RetryInfo.retryDelay`` ("34s") from the API error details, when present."""
    body = getattr(exc, "details", None)
    if not isinstance(body, dict):
        return None
    err = body.get("error", body)
    for item in (err.get("details") or []) if isinstance(err, dict) else []:
        if isinstance(item, dict) and "retryDelay" in item:
            m = _RETRY_DELAY.match(str(item["retryDelay"]))
            if m:
                return float(m.group(1))
    return None


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        transport: JsonTransport | None = None,
        base_url: str = "",
        timeout: float = 60.0,
        price_in_per_mtok: float = 0.0,
        price_out_per_mtok: float = 0.0,
        model_name: str = "gemini-1.5-flash",
        thinking: str = "off",
        client: Any = None,
    ) -> None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required")
        if thinking not in THINKING_MODES:
            raise ValueError(f"thinking must be one of {THINKING_MODES}")
        self._key = api_key
        self._t = transport
        self._timeout = timeout
        self._pin, self._pout = price_in_per_mtok, price_out_per_mtok
        self._model_name = model_name.strip() or "gemini-1.5-flash"
        self._thinking = thinking
        # The SDK client owns its HTTP stack; the injected ``client`` is for tests (no network).
        self._client = client or genai.Client(api_key=self._key, http_options=genai_types.HttpOptions(timeout=int(timeout * 1000)))

    def __repr__(self) -> str:
        return "GeminiProvider(key=***)"  # never expose the API key

    # ------------------------------------------------------------------ request building
    def thinking_mode(self, request: ProviderRequest) -> str:
        """Per-call mode: ``request.reasoning`` ("none"/"default") wins over the configured ``GEMINI_THINKING``."""
        r = (request.reasoning or "").strip().lower()
        if r in _NO_THINK:
            return "off"
        if r == "default":
            return "default"
        return self._thinking

    def _thinking_config(self, mode: str, *, fallback: bool = False) -> Any:
        if mode == "default":
            return None
        if uses_thinking_budget(self._model_name):
            budget = {"off": 0, "low": 1024}[mode]
            if fallback:
                budget = 128  # lowest budget accepted by models that cannot disable thinking (e.g. 2.5 Pro)
            return genai_types.ThinkingConfig(thinking_budget=budget)
        level = genai_types.ThinkingLevel.LOW if (mode == "low" or fallback) else genai_types.ThinkingLevel.MINIMAL
        return genai_types.ThinkingConfig(thinking_level=level)

    def _allowance(self, mode: str) -> int:
        if mode == "off":
            return 0 if uses_thinking_budget(self._model_name) else _LEVEL_MINIMAL_ALLOWANCE
        return _THINKING_ALLOWANCE[mode]

    def build_config(self, request: ProviderRequest, *, fallback: bool = False) -> Any:
        mode = self.thinking_mode(request)
        kwargs: dict[str, Any] = {
            "system_instruction": request.system or None,
            "response_mime_type": "application/json",
            "response_json_schema": compact_schema(request.response_schema),
            "temperature": request.temperature,
            "max_output_tokens": request.max_output_tokens + self._allowance(mode),
            "automatic_function_calling": genai_types.AutomaticFunctionCallingConfig(disable=True),
        }
        if (tc := self._thinking_config(mode, fallback=fallback)) is not None:
            kwargs["thinking_config"] = tc
        return genai_types.GenerateContentConfig(**kwargs)

    def _contents(self, request: ProviderRequest) -> list[Any]:
        return [
            genai_types.Content(role="model" if m["role"] == "assistant" else "user", parts=[genai_types.Part(text=m["content"])])
            for m in merge_messages(request.messages)
        ]

    # ------------------------------------------------------------------ call
    def complete(self, request: ProviderRequest) -> ProviderResponse:
        contents = self._contents(request)
        try:
            try:
                response = self._client.models.generate_content(
                    model=self._model_name, contents=contents, config=self.build_config(request))
            except genai_errors.ClientError as exc:
                if exc.code == 400 and "think" in str(exc).lower() and self.thinking_mode(request) != "default":
                    log.warning("gemini model %s rejected the thinking setting; retrying with the lowest allowed level",
                                self._model_name)
                    response = self._client.models.generate_content(
                        model=self._model_name, contents=contents, config=self.build_config(request, fallback=True))
                else:
                    raise
        except ProviderError:
            raise
        except genai_errors.APIError as exc:
            raise self._map_error(exc) from None
        except Exception as exc:  # noqa: BLE001 - SDK/network surprises must not escape as raw exceptions
            raise ProviderTransportError(f"gemini error: {type(exc).__name__}", retryable=False) from None
        return self._to_response(response)

    def _map_error(self, exc: genai_errors.APIError) -> ProviderError:
        code = int(getattr(exc, "code", 0) or 0)
        status = f"gemini API error {code}"
        if code in (401, 403):
            return ProviderAuthError(f"gemini auth/permission error {code}")
        if code == 429:
            return ProviderTransportError(f"{status} (rate limited)", retryable=True, retry_after_s=retry_delay_from_error(exc),
                                          rate_limited=True)
        if code in (408, 500, 502, 503, 504, 529):
            return ProviderTransportError(status, retryable=True, retry_after_s=retry_delay_from_error(exc))
        return ProviderTransportError(f"{status}: {str(getattr(exc, 'message', '') or '')[:200]}", retryable=False)

    def _to_response(self, response: Any) -> ProviderResponse:
        cands = getattr(response, "candidates", None) or []
        if not cands:
            fb = getattr(response, "prompt_feedback", None)
            reason = getattr(fb, "block_reason", None)
            raise ProviderTransportError(f"gemini returned no candidates{f' (blocked: {reason})' if reason else ''}",
                                         retryable=reason is None)
        cand = cands[0]
        parts = getattr(getattr(cand, "content", None), "parts", None) or []
        text = "".join(p.text for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False))
        finish = finish_reason_name(getattr(cand, "finish_reason", None))
        usage = getattr(response, "usage_metadata", None)
        prompt = int(getattr(usage, "prompt_token_count", None) or 0)
        answer = int(getattr(usage, "candidates_token_count", None) or 0)
        thoughts = int(getattr(usage, "thoughts_token_count", None) or 0)
        out_tokens = answer + thoughts  # thinking tokens are billed as output
        if not text.strip() and finish not in ("length",):
            raise ProviderTransportError(f"gemini returned no content (finish_reason={finish})", retryable=True)
        return ProviderResponse(
            content=text if text.strip() else None,
            usage=ProviderUsage(prompt, out_tokens, round((prompt * self._pin + out_tokens * self._pout) / 1_000_000, 6)),
            model=self._model_name,
            finish_reason=finish,
            metrics={
                "prompt_tokens": prompt,
                "completion_tokens": answer,
                "thoughts_tokens": thoughts,
                "total_tokens": prompt + out_tokens,
            },
        )
