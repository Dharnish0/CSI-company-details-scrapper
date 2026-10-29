#!/usr/bin/env python
"""TEST 2 — PRODUCTION EXTRACTION REQUEST.

Use the EXACT SAME production extraction request that was previously tested with Ollama and Gemini.
"""

from __future__ import annotations

import json
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


def create_production_extraction_request() -> ProviderRequest:
    """Create the exact production extraction request used in the application."""
    # This is the exact system prompt from extraction_agent.py in production
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
        model="test-model",  # Will be overridden
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
        tools=(),
        reasoning="none",
    )

    return request


def main():
    print("=" * 80)
    print("TEST 2 — PRODUCTION EXTRACTION REQUEST")
    print("=" * 80)
    print()

    settings = Settings(_env_file=".env")
    model = settings.groq_model or "openai/gpt-oss-120b"

    print(f"LLM Provider: {settings.llm_provider}")
    print(f"Groq Model: {model}")
    print(f"Groq API Key: ***{settings.groq_api_key.get_secret_value()[-4:] if settings.groq_api_key else 'not set'}")
    print()

    # Create the extraction request
    request = create_production_extraction_request()

    # Override model in request
    request_dict = vars(request)
    request_dict["model"] = model
    request_with_model = ProviderRequest(**request_dict)

    system_len = len(request_with_model.system)
    user_len = sum(len(m.content) for m in request_with_model.messages if m.content)
    schema_len = len(json.dumps(request_with_model.response_schema))
    total_len = system_len + user_len + schema_len

    print(f"Request size:")
    print(f"  System prompt: {system_len:,} chars")
    print(f"  User message: {user_len:,} chars")
    print(f"  Schema: {schema_len:,} bytes")
    print(f"  Total: {total_len:,} bytes")
    print()

    transport = TestTransport()
    provider = build_provider_from_settings(settings, transport=transport)

    print("Executing extraction request...")
    start = time.time()
    try:
        response = provider.complete(request_with_model)
        elapsed = time.time() - start

        print()
        print("REPORT:")
        print()
        print(f"Model: {response.model}")
        print(f"Request: Production extraction (13KB)")
        print(f"Latency: {elapsed*1000:.0f}ms")
        print(f"Status: SUCCESS")
        print()

        # Parse the response
        content = response.content or ""
        try:
            content_json = json.loads(content)
        except json.JSONDecodeError:
            content_json = None

        print("Details:")
        print(f"  Input tokens: {response.usage.tokens_in}")
        print(f"  Output tokens: {response.usage.tokens_out}")
        print(f"  Total tokens: {response.usage.tokens_in + response.usage.tokens_out}")
        print(f"  Finish reason: {response.finish_reason}")
        print(f"  Response size: {len(content)} chars")
        print()

        if content_json:
            print("ExtractionOutput validation:")
            print(f"  Valid JSON: YES")
            print(f"  Extracted entities: {len(content_json.get('extracted_entities', []))}")
            print(f"  Rejected candidates: {len(content_json.get('rejected_candidates', []))}")
            print()

            # Print extracted entities
            entities = content_json.get("extracted_entities", [])
            for i, entity in enumerate(entities):
                print(f"  Entity {i+1}:")
                print(f"    Company: {entity.get('company_name', 'N/A')}")
                print(f"    Code: {entity.get('company_code', 'N/A')}")
                print(f"    Parent: {entity.get('parent_company', 'N/A')}")
                print(f"    Subsidiaries: {entity.get('direct_subsidiaries', [])}")
                print(f"    Industry: {entity.get('industry', 'N/A')}")
                print(f"    Website: {entity.get('website_url', 'N/A')}")
                if entity.get("evidence_claims"):
                    print(f"    Evidence claims: {len(entity['evidence_claims'])}")
                    for claim in entity["evidence_claims"]:
                        print(f"      - {claim.get('claim', '')} (conf: {claim.get('confidence', 0):.2f})")
            print()

            # Print rejected candidates
            rejected = content_json.get("rejected_candidates", [])
            for i, candidate in enumerate(rejected):
                print(f"  Rejected candidate {i+1}:")
                print(f"    Name: {candidate.get('candidate_name', 'N/A')}")
                print(f"    Reason: {candidate.get('reason', 'N/A')}")
                print(f"    Confidence: {candidate.get('confidence', 0):.2f}")
            print()

            # Confidence stats
            all_confidences = []
            for entity in entities:
                for claim in entity.get("evidence_claims", []):
                    all_confidences.append(claim.get("confidence", 0))

            if all_confidences:
                print("Confidence statistics:")
                print(f"  Count: {len(all_confidences)}")
                print(f"  Min: {min(all_confidences):.2f}")
                print(f"  Max: {max(all_confidences):.2f}")
                print(f"  Avg: {sum(all_confidences) / len(all_confidences):.2f}")

        return 0

    except Exception as e:
        elapsed = time.time() - start
        print()
        print("REPORT:")
        print()
        print(f"Model: {model}")
        print(f"Request: Production extraction (13KB)")
        print(f"Latency: {elapsed*1000:.0f}ms")
        print(f"Status: FAILED")
        print()
        print(f"Error: {type(e).__name__}: {str(e)[:500]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
