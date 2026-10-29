#!/usr/bin/env python
"""TEST 1 - Groq Basic API Test.

Confirm:
- API authentication works
- model is available
- request succeeds
- response is returned

DO NOT print the API key.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from corpdisc.agents.ports import Message, ProviderRequest
from corpdisc.agents.providers import build_provider_from_settings
from corpdisc.config import Settings
from corpdisc.transport import HttpReply
import time


class TestTransport:
    def post(self, url, *, headers, body, timeout):
        import httpx
        response = httpx.post(url, headers=headers, content=body, timeout=timeout)
        return HttpReply(status=response.status_code, headers={k.lower(): v for k, v in response.headers.items()}, body=response.text)


def main():
    settings = Settings(_env_file=".env")
    print("=" * 80)
    print("TEST 1 — GROQ BASIC API")
    print("=" * 80)
    print()
    print(f"LLM Provider: {settings.llm_provider}")
    print(f"Groq Model: {settings.groq_model}")
    # Never print the full API key
    key_preview = "***" + settings.groq_api_key.get_secret_value()[-4:] if settings.groq_api_key else "not set"
    print(f"Groq API Key: {key_preview}")
    print()

    transport = TestTransport()
    provider = build_provider_from_settings(settings, transport=transport)

    # Simple test request
    request = ProviderRequest(
        model=settings.groq_model or "openai/gpt-oss-120b",
        system="You are a helpful assistant.",
        messages=[Message(role="user", content="Say hello")],
        response_schema={"type": "object", "properties": {"response": {"type": "string"}}},
        schema_name="SimpleResponse",
        max_output_tokens=100,
        temperature=0.0,
        tools=(),
        reasoning="none",
    )

    start = time.time()
    try:
        response = provider.complete(request)
        elapsed = time.time() - start

        print("REPORT:")
        print()
        print(f"Model: {response.model}")
        print("API authentication: OK")
        print("Request: Simple message")
        print(f"Response: {response.content[:200] if response.content else 'None'}")
        print(f"Latency: {elapsed*1000:.0f}ms")
        print("Status: SUCCESS")
        print()

        print("Details:")
        print(f"  Prompt tokens: {response.usage.tokens_in}")
        print(f"  Completion tokens: {response.usage.tokens_out}")
        print(f"  Total tokens: {response.usage.tokens_in + response.usage.tokens_out}")
        print(f"  Finish reason: {response.finish_reason}")
        print(f"  Content length: {len(response.content or '')} chars")

        return 0

    except Exception as e:
        elapsed = time.time() - start
        print("REPORT:")
        print()
        model = settings.groq_model or "openai/gpt-oss-120b"
        print(f"Model: {model}")
        print("API authentication: FAILED")
        print("Request: Simple message")
        print("Response: None")
        print(f"Latency: {elapsed*1000:.0f}ms")
        print(f"Status: FAILED")
        print()
        print(f"Error: {type(e).__name__}: {str(e)[:500]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
