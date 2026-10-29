#!/usr/bin/env python
"""Live test for Groq provider with exact production extraction request.

This script tests the Groq provider with the same extraction request used in production:
- System prompt: ~3822 chars
- User message: ~5077 chars
- Schema: ~3650 bytes
- Total: ~13KB

Usage:
    python scripts/test_groq_live.py

Environment variables:
    GROQ_API_KEY: Your Groq API key
    GROQ_MODEL: Model to use (default: llama-3.3-70b-versatile)

The script will:
1. Load settings from .env
2. Build the Groq provider
3. Create an extraction request matching production
4. Execute the request
5. Report results including timing, token usage, and cost
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Ensure we can import corpdisc
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from corpdisc.agents.ports import Message, ProviderRequest
from corpdisc.agents.providers import build_provider_from_settings
from corpdisc.config import Settings
from corpdisc.transport import JsonTransport


def create_production_extraction_request() -> ProviderRequest:
    """Create an extraction request matching the production format."""
    # This is the exact system prompt used in production (from extraction_agent.py)
    system_prompt = """You are an expert corporate hierarchy extraction assistant.
Your task is to extract company hierarchy information from the given text.
Follow these instructions carefully:

1. Read the entire input text thoroughly
2. Identify all company entities mentioned
3. Extract the following information for each company:
   - Company name
   - Company code (if available)
   - Parent company (if any)
   - Direct subsidiaries (if any)
   - Industry/sector
   - Website URL (if available)
   - Contact information (if available)

4. For each extraction, provide evidence from the text

Output Format:
Return your response as a JSON object with the following structure:
{
  "extracted_entities": [
    {
      "company_name": "...",
      "company_code": "...",
      "parent_company": "...",
      "direct_subsidiaries": ["...", "..."],
      "industry": "...",
      "website_url": "...",
      "contact_email": "...",
      "contact_phone": "...",
      "evidence_claims": [
        {
          "claim": "...",
          "evidence": "...",
          "confidence": 0.95
        }
      ]
    }
  ],
  "rejected_candidates": [
    {
      "candidate_name": "...",
      "reason": "...",
      "confidence": 0.3
    }
  ]
}

IMPORTANT:
- Only include companies explicitly mentioned in the text
- Provide specific evidence for each extraction
- Use high confidence (0.8-1.0) for explicit statements
- Use lower confidence (0.3-0.7) for inferred relationships
- Include all companies found, even if partial information
- If no companies found, return empty arrays

Be thorough and extract ALL company entities mentioned in the text.
"""

    # Sample user message matching production format
    user_message = """Extract the company hierarchy information from the following text:

Constellation Software Inc. (CSI) is a Canadian software company that acquires and manages vertical market software businesses. The company is headquartered in Toronto, Ontario.

Parent Company: Constellation Software Inc. (CSI)
- Company Code: CSI
- Industry: Software
- Website: https://www.constellation-software.com
- Contact: investor.relations@constellation-software.com

Subsidiaries:
1. CSI Software Corporation
   - Parent: Constellation Software Inc.
   - Industry: Software
   - Website: https://www.csisoftware.com
   - Contact: info@csisoftware.com

2. Constellation Software Europe
   - Parent: Constellation Software Inc.
   - Industry: Software
   - Website: https://www.constellation-software.eu
   - Contact: eu@constellation-software.com

Other entities mentioned:
- Software Company Inc. (unrelated to CSI)
- Tech Solutions Ltd. (no relationship indicated)

Please extract all company entities and their relationships.
"""

    # Create the request with the exact schema used in production
    request = ProviderRequest(
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
                            "company_code": {"type": "string"},
                            "parent_company": {"type": "string"},
                            "direct_subsidiaries": {"type": "array", "items": {"type": "string"}},
                            "industry": {"type": "string"},
                            "website_url": {"type": "string"},
                            "contact_email": {"type": "string"},
                            "contact_phone": {"type": "string"},
                            "evidence_claims": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "claim": {"type": "string"},
                                        "evidence": {"type": "string"},
                                        "confidence": {"type": "number"}
                                    }
                                }
                            }
                        }
                    }
                },
                "rejected_candidates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "candidate_name": {"type": "string"},
                            "reason": {"type": "string"},
                            "confidence": {"type": "number"}
                        }
                    }
                }
            }
        },
        schema_name="ExtractionOutput",
        max_output_tokens=4096,
        temperature=0.0,
        tools=None,
        reasoning="none",
    )

    return request


def main() -> int:
    """Main function to run the Groq live test."""
    print("=" * 80)
    print("Groq Live API Test - Production Extraction Request")
    print("=" * 80)

    # Load settings from .env
    settings = Settings(_env_file=".env")
    print(f"\nConfiguration:")
    print(f"  LLM Provider: {settings.llm_provider or 'not set'}")
    print(f"  Groq API Key: {'***' + settings.groq_api_key.get_secret_value()[-4:] if settings.groq_api_key else 'not set'}")
    print(f"  Groq Model: {settings.groq_model or 'default'}")
    print(f"  Timeout: {settings.llm_timeout_seconds}s")

    # Check if Groq API key is set
    if not settings.groq_api_key:
        print("\n❌ ERROR: GROQ_API_KEY is not set in .env file")
        print("Please set GROQ_API_KEY in your .env file and try again.")
        return 1

    # Create the extraction request
    print("\n" + "-" * 80)
    print("Creating extraction request...")
    request = create_production_extraction_request()

    system_len = len(request.system)
    user_len = sum(len(m.content) for m in request.messages if m.content)
    schema_len = len(json.dumps(request.response_schema))

    print(f"  System prompt length: {system_len:,} chars")
    print(f"  User message length: {user_len:,} chars")
    print(f"  Schema length: {schema_len:,} bytes")
    print(f"  Total estimated: {system_len + user_len + schema_len:,} bytes")
    print(f"  Schema name: {request.schema_name}")
    print(f"  Max output tokens: {request.max_output_tokens}")

    # Build the Groq provider
    print("\n" + "-" * 80)
    print("Building Groq provider...")
    try:
        from corpdisc.transport import JsonTransport

        class SimpleTransport(JsonTransport):
            def post(self, url: str, headers: dict, body: str, timeout: float) -> dict:
                import httpx
                response = httpx.post(url, headers=headers, content=body, timeout=timeout)
                return {
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                    "body": response.text,
                }

        transport = SimpleTransport()
        provider = build_provider_from_settings(settings, transport=transport)
        print(f"  Provider: {provider.name}")
        print(f"  Model: {provider._model_name if hasattr(provider, '_model_name') else 'default'}")
    except Exception as e:
        print(f"❌ ERROR building provider: {e}")
        return 1

    # Execute the request
    print("\n" + "-" * 80)
    print("Executing extraction request...")
    import time

    start_time = time.time()
    try:
        response = provider.complete(request)
        elapsed = time.time() - start_time

        # Print results
        print(f"\n✅ SUCCESS")
        print(f"  Response received in: {elapsed:.2f}s")
        print(f"  Model: {response.model}")
        print(f"  Finish reason: {response.finish_reason}")
        print(f"  Prompt tokens: {response.usage.prompt_tokens:,}")
        print(f"  Completion tokens: {response.usage.completion_tokens:,}")
        print(f"  Total tokens: {response.usage.total_tokens:,}")

        # Calculate cost if prices are configured
        if settings.llm_price_in_per_mtok > 0 or settings.llm_price_out_per_mtok > 0:
            cost = response.usage.cost
            print(f"  Estimated cost: ${cost:.6f}")

        # Print metrics if available
        if hasattr(response, "metrics") and response.metrics:
            print(f"\n  Metrics:")
            for key, value in response.metrics.items():
                if key != "request_bytes":  # Skip request bytes to avoid clutter
                    print(f"    {key}: {value}")

        # Print extracted content
        print(f"\n  Extracted content ({len(response.content or '')} chars):")
        if response.content:
            try:
                content_json = json.loads(response.content)
                print(f"    Extracted entities: {len(content_json.get('extracted_entities', []))}")
                print(f"    Rejected candidates: {len(content_json.get('rejected_candidates', []))}")
            except json.JSONDecodeError:
                print(f"    {response.content[:500]}...")

        return 0

    except Exception as e:
        elapsed = time.time() - start_time
        print(f"\n❌ ERROR after {elapsed:.2f}s")
        print(f"  Type: {type(e).__name__}")
        print(f"  Message: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
