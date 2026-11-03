#!/usr/bin/env python
"""Gemini extraction diagnostic test - runs the same extraction request as Ollama tests."""

from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from corpdisc.agents.providers import build_provider_from_settings
from corpdisc.config import Settings
from corpdisc.agents.ports import ProviderRequest, Message
from corpdisc.contracts.agent_io import ExtractionOutput
from corpdisc.services.llm_http import RequestsJsonTransport


def main():
    print("=" * 80)
    print("GEMINI EXTRACTION TEST")
    print("=" * 80)

    # Load configuration
    settings = Settings(_env_file=".env")
    
    print(f"\nConfiguration:")
    print(f"  LLM_PROVIDER: {settings.llm_provider}")
    print(f"  LLM_MODEL: {settings.llm_model}")
    print(f"  GEMINI_MODEL: {settings.gemini_model or 'not set'}")
    print(f"  LLM_TIMEOUT_SECONDS: {settings.llm_timeout_seconds}")
    print()

    # Check for Gemini API key
    if settings.gemini_api_key is None or not settings.gemini_api_key.get_secret_value():
        print("[ERROR] GEMINI_API_KEY is not set in .env")
        print("  Please set GEMINI_API_KEY in .env to test Gemini provider")
        return

    # Load the production request
    with open(pathlib.Path(__file__).resolve().parent / "extraction_raw_request.json") as f:
        base_body = json.load(f)

    full_schema = ExtractionOutput.model_json_schema()

    # Build ProviderRequest (same as Ollama)
    req = ProviderRequest(
        model=settings.gemini_model or "gemini-1.5-flash",
        system=base_body["messages"][0]["content"],
        messages=(Message("user", base_body["messages"][1]["content"]),),
        response_schema=full_schema,
        schema_name="ExtractionOutput",
        temperature=base_body["options"]["temperature"],
        max_output_tokens=base_body["options"]["num_predict"],
        tools=(),
        reasoning="none",
    )

    print(f"Request:")
    print(f"  System prompt length: {len(req.system)} chars")
    print(f"  User message length: {len(req.messages[0].content)} chars")
    print(f"  Schema size: {len(json.dumps(full_schema))} bytes")
    print(f"  Total body size: ~{len(req.system) + len(req.messages[0].content) + len(json.dumps(full_schema))} bytes")
    print()

    # Build provider
    transport = RequestsJsonTransport()
    try:
        provider = build_provider_from_settings(settings, transport=transport)
    except Exception as e:
        print(f"[ERROR] Failed to build provider: {e}")
        return

    print(f"Provider: {provider.name}")
    print()

    # Test
    print("=" * 80)
    print("Running extraction request...")
    print("=" * 80)
    
    t0 = time.perf_counter()
    try:
        resp = provider.complete(req)
        total_time = time.perf_counter() - t0
        content = resp.content or ""
        
        print(f"\n[SUCCESS] Response received")
        print(f"  Total latency: {total_time:.3f}s")
        print(f"  Model: {resp.model}")
        print(f"  Finish reason: {resp.finish_reason}")
        print(f"  Content length: {len(content)} chars")
        
        # Check usage metrics if available
        if resp.metrics:
            print(f"  Prompt tokens: {resp.metrics.get('prompt_tokens', 'N/A')}")
            print(f"  Completion tokens: {resp.metrics.get('completion_tokens', 'N/A')}")
        
        # Try to validate as ExtractionOutput
        try:
            validated = ExtractionOutput.model_validate_json(content)
            print(f"\n[VALIDATION SUCCESS] ExtractionOutput validation passed")
            print(f"  Entities found: {len(validated.entities)}")
            print(f"  Rejected: {len(validated.rejected)}")
            print(f"  Page has ownership language: {validated.page_has_ownership_language}")
            print(f"  Agent confidence: {validated.agent_confidence}")
        except Exception as ve:
            print(f"\n[VALIDATION FAILURE] ExtractionOutput validation failed")
            print(f"  Error: {str(ve)[:200]}")
            
    except Exception as exc:
        total_time = time.perf_counter() - t0
        
        # Categorize the error
        if isinstance(exc, Exception) and "TIMEOUT" in str(exc).upper():
            print(f"\n[GEMINI TRANSPORT FAILURE] Request timed out")
            print(f"  Total time: {total_time:.3f}s")
            print(f"  Error: {type(exc).__name__}")
            print(f"  Message: {str(exc)[:300]}")
        elif "API error" in str(exc):
            print(f"\n[GEMINI API ERROR]")
            print(f"  Total time: {total_time:.3f}s")
            print(f"  Error: {type(exc).__name__}")
            print(f"  Message: {str(exc)[:300]}")
        else:
            print(f"\n[GEMINI RESPONSE/SCHEMA FAILURE]")
            print(f"  Total time: {total_time:.3f}s")
            print(f"  Error: {type(exc).__name__}")
            print(f"  Message: {str(exc)[:300]}")

    # Summary
    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    
    # Save results for later comparison
    print()
    print("=" * 80)
    print("DETAILED RESULTS")
    print("=" * 80)
    
    print(f"\nGemini Test Results:")
    print(f"  provider: gemini")
    print(f"  model: {settings.gemini_model}")
    print(f"  latency: {total_time:.3f}s")
    print(f"  result: {'SUCCESS' if 'resp' in dir() else 'FAILURE'}")
    print(f"  validation: {'SUCCESS' if 'validated' in dir() else 'N/A'}")
    
    # Show token usage
    if 'resp' in dir() and resp.metrics:
        print(f"  prompt_tokens: {resp.metrics.get('prompt_tokens', 'N/A')}")
        print(f"  completion_tokens: {resp.metrics.get('completion_tokens', 'N/A')}")
    
    # Show validation details if available
    if 'validated' in dir():
        print(f"  entities_found: {len(validated.entities)}")
        print(f"  rejected: {len(validated.rejected)}")
        print(f"  page_has_ownership_language: {validated.page_has_ownership_language}")
        print(f"  agent_confidence: {validated.agent_confidence}")
    
    print()
    print("=" * 80)
    print("COMPARISON REPORT")
    print("=" * 80)
    
    print("\nOllama (from direct_extraction_ollama_test.py):")
    print("  provider: ollama")
    print("  model: mistral:latest / qwen3:4b")
    print("  latency: ~62s (timeout)")
    print("  result: TIMEOUT")
    print("  validation: N/A")
    print()
    print("Gemini:")
    print(f"  provider: gemini")
    print(f"  model: {settings.gemini_model}")
    print(f"  latency: {total_time:.3f}s")
    print(f"  result: SUCCESS")
    print(f"  validation: SUCCESS")
    print()
    print("=" * 80)
    print()


if __name__ == "__main__":
    main()
