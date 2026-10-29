#!/usr/bin/env python
"""Test Groq with/without JSON schema to understand the error."""

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


def test_with_schema():
    """Test with JSON schema (structured output)."""
    settings = Settings(_env_file=".env")
    model = settings.groq_model or "openai/gpt-oss-120b"

    transport = TestTransport()
    provider = build_provider_from_settings(settings, transport=transport)

    # Test with JSON schema (structured output)
    request = ProviderRequest(
        model=model,
        system="You are a helpful assistant.",
        messages=[Message(role="user", content="Output JSON: {\"response\": \"hello\"}")],
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
        return True, elapsed, response
    except Exception as e:
        elapsed = time.time() - start
        return False, elapsed, e


def test_without_schema():
    """Test without JSON schema (plain text)."""
    settings = Settings(_env_file=".env")
    model = settings.groq_model or "openai/gpt-oss-120b"

    transport = TestTransport()
    provider = build_provider_from_settings(settings, transport=transport)

    # Test without JSON schema
    request = ProviderRequest(
        model=model,
        system="You are a helpful assistant. Output plain text, not JSON.",
        messages=[Message(role="user", content="Say hello")],
        response_schema=None,  # No schema
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
        return True, elapsed, response
    except Exception as e:
        elapsed = time.time() - start
        return False, elapsed, e


def main():
    print("=" * 80)
    print("Groq Schema Test")
    print("=" * 80)
    print()

    print("Test 1: With JSON schema (structured output)")
    print("-" * 40)
    success, elapsed, result = test_with_schema()
    if success:
        print(f"Status: SUCCESS")
        print(f"Latency: {elapsed*1000:.0f}ms")
        print(f"Response: {result.content}")
    else:
        print(f"Status: FAILED")
        print(f"Latency: {elapsed*1000:.0f}ms")
        print(f"Error: {type(result).__name__}: {str(result)[:500]}")

    print()
    print("Test 2: Without JSON schema (plain text)")
    print("-" * 40)
    success, elapsed, result = test_without_schema()
    if success:
        print(f"Status: SUCCESS")
        print(f"Latency: {elapsed*1000:.0f}ms")
        print(f"Response: {result.content}")
    else:
        print(f"Status: FAILED")
        print(f"Latency: {elapsed*1000:.0f}ms")
        print(f"Error: {type(result).__name__}: {str(result)[:500]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
