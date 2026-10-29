#!/usr/bin/env python
"""Compare Ollama, Gemini, and Groq providers with the same extraction request.

This script runs the same extraction request through all three providers
and compares:
- Success/failure rate
- Response time
- Token usage
- Cost (if configured)
- Error types

Usage:
    python scripts/compare_providers.py

Environment variables:
    OLLAMA_BASE_URL: Ollama endpoint (default: http://localhost:11434)
    GEMINI_API_KEY: Gemini API key (required for Gemini)
    GEMINI_MODEL: Gemini model (default: gemini-1.5-flash)
    GROQ_API_KEY: Groq API key (required for Groq)
    GROQ_MODEL: Groq model (default: llama-3.3-70b-versatile)
    LLM_TIMEOUT_SECONDS: Request timeout in seconds (default: 60)

Note: Ollama runs locally without an API key. Gemini and Groq require API keys.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Ensure we can import corpdisc
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from corpdisc.agents.ports import Message, ProviderRequest
from corpdisc.agents.providers import build_provider_from_settings
from corpdisc.config import Settings
from corpdisc.transport import HttpReply, JsonTransport


@dataclass
class ProviderTestResult:
    """Result of testing a provider."""
    provider: str
    success: bool
    elapsed_ms: float
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cost_usd: float | None
    error_type: str | None
    error_message: str | None
    content_length: int | None
    finish_reason: str | None


def create_test_extraction_request() -> ProviderRequest:
    """Create a test extraction request."""
    system_prompt = """You are an expert corporate hierarchy extraction assistant.
Extract company hierarchy information from the given text."""

    user_message = """Extract company entities from:

Constellation Software Inc. (CSI) is a Canadian software company.
They have subsidiaries including CSI Software Corporation and Constellation Software Europe.
Other entities mentioned: Software Company Inc. (unrelated) and Tech Solutions Ltd.

Return a JSON object with extracted_entities array containing company information."""

    return ProviderRequest(
        model="test-model",  # Will be overridden by provider
        system=system_prompt,
        messages=[Message(role="user", content=user_message)],
        response_schema={
            "type": "object",
            "properties": {
                "extracted_entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "company_name": {"type": "string"},
                            "subsidiaries": {"type": "array", "items": {"type": "string"}},
                        }
                    }
                }
            }
        },
        schema_name="TestExtraction",
        max_output_tokens=1024,
        temperature=0.0,
        tools=(),
        reasoning="none",
    )


class SimpleTransport(JsonTransport):
    """Simple HTTP transport using httpx."""

    def post(self, url: str, *, headers: dict[str, str], body: str, timeout: float) -> HttpReply:
        import httpx
        response = httpx.post(url, headers=headers, content=body, timeout=timeout)
        return HttpReply(
            status=response.status_code,
            headers={k.lower(): v for k, v in response.headers.items()},
            body=response.text,
        )


def test_provider(provider_name: str, settings: Settings, request: ProviderRequest) -> ProviderTestResult:
    """Test a single provider with the given request."""
    print(f"\nTesting {provider_name}...")

    transport = SimpleTransport()

    # Override model for the request based on provider
    request_dict = vars(request)
    if provider_name == "ollama":
        request_dict["model"] = settings.llm_model or "qwen3:4b"
    elif provider_name == "gemini":
        request_dict["model"] = settings.gemini_model or "gemini-1.5-flash"
    elif provider_name == "groq":
        request_dict["model"] = settings.groq_model or "llama-3.3-70b-versatile"

    request_with_model = ProviderRequest(**request_dict)

    try:
        provider = build_provider_from_settings(settings, transport=transport)
        start = time.time()
        response = provider.complete(request_with_model)
        elapsed_ms = (time.time() - start) * 1000

        return ProviderTestResult(
            provider=provider_name,
            success=True,
            elapsed_ms=elapsed_ms,
            prompt_tokens=response.usage.tokens_in,
            completion_tokens=response.usage.tokens_out,
            total_tokens=response.usage.tokens_in + response.usage.tokens_out,
            cost_usd=response.usage.cost_usd,
            error_type=None,
            error_message=None,
            content_length=len(response.content or ""),
            finish_reason=response.finish_reason,
        )
    except Exception as e:
        elapsed_ms = (time.time() - time.time()) * 1000  # Will be 0, not useful for errors
        return ProviderTestResult(
            provider=provider_name,
            success=False,
            elapsed_ms=0,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            cost_usd=None,
            error_type=type(e).__name__,
            error_message=str(e),
            content_length=None,
            finish_reason=None,
        )


def print_result(result: ProviderTestResult) -> None:
    """Print a test result."""
    status = "✅ SUCCESS" if result.success else "❌ FAILED"
    print(f"\n  {status} [{result.provider}]")
    print(f"    Time: {result.elapsed_ms:.0f}ms")
    if result.success:
        print(f"    Tokens: {result.total_tokens} ({result.prompt_tokens} in, {result.completion_tokens} out)")
        print(f"    Content: {result.content_length} chars")
        print(f"    Finish: {result.finish_reason}")
        if result.cost_usd is not None:
            print(f"    Cost: ${result.cost_usd:.6f}")
    else:
        print(f"    Error: {result.error_type}: {result.error_message}")


def main() -> int:
    """Main function to compare providers."""
    print("=" * 80)
    print("Provider Comparison Test")
    print("=" * 80)

    # Load settings from .env
    settings = Settings(_env_file=".env")
    print(f"\nConfiguration:")
    print(f"  LLM Provider: {settings.llm_provider or 'not set'}")
    print(f"  Ollama Model: {settings.llm_model or 'default'}")
    print(f"  Ollama Num CTX: {settings.ollama_num_ctx}")
    print(f"  Ollama Keep Alive: {settings.ollama_keep_alive}")
    print(f"  Gemini Model: {settings.gemini_model or 'default'}")
    print(f"  Groq Model: {settings.groq_model or 'default'}")
    print(f"  Timeout: {settings.llm_timeout_seconds}s")

    # Check for required API keys
    gemini_key = settings.gemini_api_key
    groq_key = settings.groq_api_key

    if not gemini_key:
        print("\n⚠️  WARNING: GEMINI_API_KEY not set. Skipping Gemini test.")
    if not groq_key:
        print("\n⚠️  WARNING: GROQ_API_KEY not set. Skipping Groq test.")

    # Create test request
    request = create_test_extraction_request()

    # Test each provider
    results: list[ProviderTestResult] = []

    # Always test Ollama (local, no key needed) - use qwen3:4b which is available locally
    ollama_settings = Settings(
        _env_file=None,
        db_path="x.db",
        cache_dir="c",
        log_dir="l",
        llm_provider="ollama",
        llm_model="qwen3:4b",
        llm_base_url=settings.llm_base_url or "http://localhost:11434",
        llm_timeout_seconds=settings.llm_timeout_seconds,
        ollama_num_ctx=settings.ollama_num_ctx,
        ollama_keep_alive=settings.ollama_keep_alive,
    )
    results.append(test_provider("ollama", ollama_settings, request))

    # Test Gemini if key is available - use gemini-1.5-flash which is more stable
    if gemini_key:
        gemini_settings = Settings(
            _env_file=None,
            db_path="x.db",
            cache_dir="c",
            log_dir="l",
            llm_provider="gemini",
            gemini_api_key=gemini_key,
            gemini_model="gemini-1.5-flash",
            llm_timeout_seconds=settings.llm_timeout_seconds,
        )
        results.append(test_provider("gemini", gemini_settings, request))

    # Test Groq if key is available - use llama3-1-8b which is commonly available
    if groq_key:
        groq_settings = Settings(
            _env_file=None,
            db_path="x.db",
            cache_dir="c",
            log_dir="l",
            llm_provider="groq",
            groq_api_key=groq_key,
            groq_model="llama3-1-8b",
            llm_timeout_seconds=settings.llm_timeout_seconds,
        )
        results.append(test_provider("groq", groq_settings, request))

    # Print results summary
    print("\n" + "=" * 80)
    print("Results Summary")
    print("=" * 80)

    for result in results:
        print_result(result)

    # Summary statistics
    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success]

    print("\n" + "-" * 80)
    print(f"Total: {len(results)} providers tested")
    print(f"  Success: {len(successes)}")
    print(f"  Failed: {len(failures)}")

    if successes:
        avg_time = sum(r.elapsed_ms for r in successes) / len(successes)
        print(f"\n  Average response time: {avg_time:.0f}ms")

    # Recommendations
    print("\n" + "-" * 80)
    print("Recommendations")
    print("-" * 80)

    if not gemini_key:
        print("\n1. Add GEMINI_API_KEY to .env to test Gemini")
    if not groq_key:
        print("\n2. Add GROQ_API_KEY to .env to test Groq")

    if failures:
        print(f"\n3. {len(failures)} provider(s) failed. Check API keys and network connectivity.")
    else:
        print("\n3. All tested providers are working correctly.")

    print("\n" + "=" * 80)

    # Return 0 if Ollama works (local fallback available), else 1
    ollama_success = any(r.provider == "ollama" and r.success for r in results)
    return 0 if ollama_success else 1


if __name__ == "__main__":
    sys.exit(main())
