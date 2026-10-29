#!/usr/bin/env python
"""Trace the exact exception path through the application."""

from __future__ import annotations

import json
import pathlib
import sys
import time
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import requests

from corpdisc.services.llm_http import RequestsJsonTransport
from corpdisc.agents.providers.ollama import OllamaProvider
from corpdisc.agents.ports import ProviderRequest, Message
from corpdisc.contracts.agent_io import ExtractionOutput


class InstrumentedTransport:
    """Transport wrapper that captures all details."""
    
    def __init__(self, transport):
        self._transport = transport
        self.calls = []
    
    def post(self, url, *, headers, body, timeout):
        print(f"\n  Transport.post called:")
        print(f"    URL: {url}")
        print(f"    Headers: {headers}")
        print(f"    Body size: {len(body)} bytes")
        print(f"    Timeout: {timeout}s")
        
        try:
            result = self._transport.post(url, headers=headers, body=body, timeout=timeout)
            print(f"    Transport succeeded: status={result.status}, body size={len(result.body)} bytes")
            return result
        except Exception as exc:
            print(f"    Transport failed: {type(exc).__module__}.{type(exc).__name__}: {str(exc)[:200]}")
            raise


def main():
    print("=" * 80)
    print("EXCEPTION TRACE - Full Path Analysis")
    print("=" * 80)
    
    # Load real request
    with open("diagnostics/extraction_raw_request.json") as f:
        real_req = json.load(f)
    
    model = real_req["model"]
    system = real_req["messages"][0]["content"]
    user = real_req["messages"][1]["content"]
    options = real_req["options"]
    
    print(f"\nReal extraction request characteristics:")
    print(f"  Model: {model}")
    print(f"  System prompt: {len(system)} chars")
    print(f"  User message: {len(user)} chars")
    print(f"  Total body: {len(json.dumps(real_req))} bytes")
    print(f"  num_ctx: {options['num_ctx']}, num_predict: {options['num_predict']}")
    
    # Test with short timeout first
    print("\n" + "=" * 80)
    print("TEST 1: Tiny request through JsonTransport (SHORT TIMEOUT)")
    print("=" * 80)
    
    transport = RequestsJsonTransport()
    instrumented = InstrumentedTransport(transport)
    
    schema = ExtractionOutput.model_json_schema()
    system_tiny = "You are an agent."
    messages_tiny = [Message("user", "Return OK")]
    
    provider = OllamaProvider(
        transport=instrumented,
        base_url="",
        timeout=60,
        num_ctx=12288,
        keep_alive="30m"
    )
    
    req_tiny = ProviderRequest(
        model=model,
        system=system_tiny,
        messages=messages_tiny,
        response_schema=schema,
        schema_name="ExtractionOutput",
        temperature=0.0,
        max_output_tokens=100,
        tools=(),
        reasoning="none",
    )
    
    print("\nMaking tiny request with 60s timeout...")
    t0 = time.perf_counter()
    try:
        resp = provider.complete(req_tiny)
        wall = time.perf_counter() - t0
        print(f"\nSUCCESS! Time: {wall:.1f}s, content: {len(resp.content or '')} chars")
    except Exception as exc:
        wall = time.perf_counter() - t0
        print(f"\nEXCEPTION after {wall:.1f}s:")
        print(f"  Type: {type(exc).__module__}.{type(exc).__name__}")
        print(f"  Message: {str(exc)}")
        print(f"  Retryable: {getattr(exc, 'retryable', 'N/A')}")
        traceback.print_exc()
    
    # Test with real extraction (SHORT TIMEOUT - 30s)
    print("\n" + "=" * 80)
    print("TEST 2: Real extraction with 30s timeout")
    print("=" * 80)
    
    transport2 = RequestsJsonTransport()
    instrumented2 = InstrumentedTransport(transport2)
    
    provider2 = OllamaProvider(
        transport=instrumented2,
        base_url="",
        timeout=30,
        num_ctx=options["num_ctx"],
        keep_alive="30m"
    )
    
    req_real = ProviderRequest(
        model=model,
        system=system,
        messages=[Message("user", user)],
        response_schema=schema,
        schema_name="ExtractionOutput",
        temperature=options["temperature"],
        max_output_tokens=options["num_predict"],
        tools=(),
        reasoning="none",
    )
    
    print("\nMaking real extraction request with 30s timeout...")
    t0 = time.perf_counter()
    try:
        resp = provider2.complete(req_real)
        wall = time.perf_counter() - t0
        print(f"\nSUCCESS! Time: {wall:.1f}s, content: {len(resp.content or '')} chars")
    except Exception as exc:
        wall = time.perf_counter() - t0
        print(f"\nEXCEPTION after {wall:.1f}s:")
        print(f"  Type: {type(exc).__module__}.{type(exc).__name__}")
        print(f"  Message: {str(exc)}")
        print(f"  Retryable: {getattr(exc, 'retryable', 'N/A')}")
        traceback.print_exc()
        
        # Check if it's a TransportFailure
        if hasattr(exc, '__cause__') and exc.__cause__:
            print(f"\n  CAUSE:")
            print(f"    Type: {type(exc.__cause__).__module__}.{type(exc.__cause__).__name__}")
            print(f"    Message: {str(exc.__cause__)}")
        
        # Check TransportFailure attributes
        from corpdisc.transport import TransportFailure
        if isinstance(exc, TransportFailure):
            print(f"\n  TransportFailure.code: {exc.code}")
            print(f"  TransportFailure.message: {exc.args[0]}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
