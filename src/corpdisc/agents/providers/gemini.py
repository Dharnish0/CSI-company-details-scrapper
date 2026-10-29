"""Google Gemini API adapter (no SDK, D16). Uses Gemini's native structured output support via
response_schema parameter and Pydantic validation for schema repair.

Gemini's structured output is requested via response_schema (JSON Schema) which is more reliable than
prompt-based approaches. Verified downstream by AgentRunner's Pydantic validation.
"""

from __future__ import annotations

import json
from typing import Any

import google.genai as genai
from google.api_core.exceptions import GoogleAPIError, NotFound, ServiceUnavailable

from corpdisc.agents.ports import (
    Message,
    ProviderRequest,
    ProviderResponse,
    ProviderTransportError,
    ProviderUsage,
)
from corpdisc.transport import JsonTransport


_NO_THINK = frozenset({"none", "off", "false", "disabled"})


def compact_schema(schema: Any) -> Any:
    """Clean a pydantic JSON Schema for Gemini API - remove unsupported fields."""
    if isinstance(schema, dict):
        return {k: compact_schema(v) for k, v in schema.items() if k not in ("title", "default", "additionalProperties")}
    if isinstance(schema, list):
        return [compact_schema(v) for v in schema]
    return schema


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        transport: JsonTransport,
        base_url: str = "",
        timeout: float = 60.0,
        price_in_per_mtok: float = 0.0,
        price_out_per_mtok: float = 0.0,
        model_name: str = "gemini-1.5-flash",
    ) -> None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required")
        self._key = api_key
        self._t = transport
        self._timeout = timeout
        self._pin, self._pout = price_in_per_mtok, price_out_per_mtok
        self._model_name = model_name.strip() or "gemini-1.5-flash"

        # Initialize the Gemini client with API key
        self._client = genai.Client(api_key=self._key)

    def __repr__(self) -> str:
        # Never expose the API key
        return "GeminiProvider(key=***)"

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Execute a Gemini API call with structured output.

        Gemini's structured output uses response_schema parameter with the JSON Schema.
        The content is returned as a string that must be parsed as JSON.
        """
        # In google.genai SDK:
        # - system_instruction goes in GenerateContentConfig
        # - contents is a list of user/assistant messages
        system_instruction = request.system
        contents = [msg.content for msg in request.messages]

        try:
            # Configure generation settings with response schema and system instruction
            generation_config = genai.types.GenerateContentConfig(
                system_instruction=system_instruction if system_instruction else None,
                response_schema=compact_schema(request.response_schema),
                response_mime_type="application/json",
                temperature=request.temperature,
                max_output_tokens=request.max_output_tokens,
            )

            # Make the API call
            response = self._client.models.generate_content(
                model=self._model_name,
                contents=contents,
                config=generation_config,
            )

            # Parse response
            if not response.candidates or not response.candidates[0].content.parts:
                raise ProviderTransportError("gemini returned no content", retryable=True)

            content = response.candidates[0].content.parts[0].text if response.candidates[0].content.parts else ""

            # Extract usage stats from response
            prompt_tokens = 0
            completion_tokens = 0
            if response.usage_metadata:
                prompt_tokens = response.usage_metadata.prompt_token_count
                completion_tokens = response.usage_metadata.candidates_token_count

            return ProviderResponse(
                content=content if content.strip() else None,
                usage=ProviderUsage(prompt_tokens, completion_tokens, round((prompt_tokens * self._pin + completion_tokens * self._pout) / 1_000_000, 6)),
                model=self._model_name,
                finish_reason=self._normalize_finish_reason(response.candidates[0].finish_reason if response.candidates else None),
                metrics={
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            )

        except (GoogleAPIError, NotFound, ServiceUnavailable) as e:
            # API errors - some are retryable, some are not
            retryable = isinstance(e, ServiceUnavailable)  # 503 is retryable
            raise ProviderTransportError(f"gemini API error: {e}", retryable=retryable) from e
        except Exception as e:
            # Unexpected errors
            raise ProviderTransportError(f"gemini error: {e}", retryable=False) from e

    def _normalize_finish_reason(self, finish_reason: Any) -> str | None:
        """Normalize Gemini finish reason to our standard."""
        if finish_reason is None:
            return None
        # Gemini uses: FINISH_REASON_STOP, FINISH_REASON_MAX_TOKENS, etc.
        reason_str = str(finish_reason).upper()
        if "STOP" in reason_str:
            return "stop"
        elif "MAX" in reason_str or "LENGTH" in reason_str:
            return "length"
        elif "SAFETY" in reason_str:
            return "safety"
        else:
            return reason_str.lower()
