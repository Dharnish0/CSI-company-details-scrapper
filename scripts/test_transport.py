#!/usr/bin/env python
"""Test the exact transport with the real request."""

from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import requests
import httpx

from corpdisc.services.llm_http import RequestsJsonTransport
from corpdisc.agents.providers.ollama import OllamaProvider
from corpdisc.agents.ports import ProviderRequest, Message
from corpdisc.contracts.agent_io import ExtractionOutput


def test_with_requests(model, messages, options, timeout, use_stream=False):
    """Test using raw requests library."""
    base_url = "http://localhost:11434"
    
    body = {
        "model": model,
        "messages": messages,
        "options": options,
        "stream": use_stream,
        "keep_alive": "30m",
    }
    
    print(f"  requests (stream={use_stream}):", end=" ")
    t0 = time.perf_counter()
    try:
        r = requests.post(f"{base_url}/api/chat", json=body, timeout=timeout, stream=use_stream)
        wall = time.perf_counter() - t0
        print(f"status={r.status_code}, time={wall:.1f}s", end="")
        
        if use_stream:
            chunks = list(r.iter_content(chunk_size=65536))
            text = b"".join(chunks).decode("utf-8")
        else:
            text = r.text
        data = json.loads(text)
        
        content = data.get("message", {}).get("content", "")
        print(f", content={len(content)} chars")
        return {
            "status": "OK",
            "wall_s": wall,
            "content_length": len(content),
            "done_reason": data.get("done_reason"),
            "exception": None,
        }
    except requests.Timeout as exc:
        wall = time.perf_counter() - t0
        print(f"TIMEOUT after {wall:.1f}s")
        return {
            "status": "TIMEOUT",
            "wall_s": wall,
            "exception": f"requests.Timeout: {type(exc).__name__}",
        }
    except Exception as exc:
        wall = time.perf_counter() - t0
        print(f"{type(exc).__name__} after {wall:.1f}s")
        return {
            "status": type(exc).__name__,
            "wall_s": wall,
            "exception": f"{type(exc).__name__}: {str(exc)[:200]}",
        }


def test_with_httpx(model, messages, options, timeout, use_stream=False):
    """Test using httpx library."""
    base_url = "http://localhost:11434"
    
    body = {
        "model": model,
        "messages": messages,
        "options": options,
    }
    
    print(f"  httpx (stream={use_stream}):", end=" ")
    t0 = time.perf_counter()
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
            r = client.post(f"{base_url}/api/chat", json=body, headers={"content-type": "application/json"})
            wall = time.perf_counter() - t0
            print(f"status={r.status_code}, time={wall:.1f}s", end="")
            
            data = r.json()
            content = data.get("message", {}).get("content", "")
            print(f", content={len(content)} chars")
            return {
                "status": "OK",
                "wall_s": wall,
                "content_length": len(content),
                "done_reason": data.get("done_reason"),
                "exception": None,
            }
    except httpx.TimeoutException as exc:
        wall = time.perf_counter() - t0
        print(f"TIMEOUT after {wall:.1f}s")
        return {
            "status": "TIMEOUT",
            "wall_s": wall,
            "exception": f"httpx.TimeoutException: {type(exc).__name__}",
        }
    except Exception as exc:
        wall = time.perf_counter() - t0
        print(f"{type(exc).__name__} after {wall:.1f}s")
        return {
            "status": type(exc).__name__,
            "wall_s": wall,
            "exception": f"{type(exc).__name__}: {str(exc)[:200]}",
        }


def test_with_json_transport(model, system, messages, max_output_tokens, num_ctx, timeout):
    """Test using the application's RequestsJsonTransport."""
    transport = RequestsJsonTransport()
    
    schema = ExtractionOutput.model_json_schema()
    req = ProviderRequest(
        model=model,
        system=system,
        messages=messages,
        response_schema=schema,
        schema_name="ExtractionOutput",
        temperature=0.0,
        max_output_tokens=max_output_tokens,
        tools=(),
        reasoning="none",
    )
    
    provider = OllamaProvider(transport=transport, base_url="", timeout=timeout, num_ctx=num_ctx, keep_alive="30m")
    
    print(f"  JsonTransport:", end=" ")
    t0 = time.perf_counter()
    try:
        resp = provider.complete(req)
        wall = time.perf_counter() - t0
        content = resp.content or ""
        print(f"OK, time={wall:.1f}s, content={len(content)} chars")
        return {
            "status": "OK",
            "wall_s": wall,
            "content_length": len(content),
            "done_reason": resp.finish_reason,
            "exception": None,
        }
    except Exception as exc:
        wall = time.perf_counter() - t0
        print(f"{type(exc).__name__} after {wall:.1f}s")
        return {
            "status": type(exc).__name__,
            "wall_s": wall,
            "exception": f"{type(exc).__module__}.{type(exc).__name__}: {str(exc)[:300]}",
        }


def main():
    print("=" * 80)
    print("TRANSPORT INVESTIGATION - Exact Exception Capture")
    print("=" * 80)
    
    # Load the real extraction request
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
    print(f"  num_ctx: {options['num_ctx']}, num_predict: {options['num_predict']}")
    
    print("\n" + "=" * 80)
    print("EXPERIMENT 1: Tiny request with all transports")
    print("=" * 80)
    
    tiny_messages = [{"role": "user", "content": "Return OK"}]
    tiny_options = {"temperature": 0.0, "num_predict": 10, "num_ctx": 12288}
    
    print("\nTiny request (10 tokens):")
    test_with_requests(model, tiny_messages, tiny_options, 60, use_stream=True)
    test_with_httpx(model, tiny_messages, tiny_options, 60, use_stream=True)
    
    # Test with JsonTransport
    schema = ExtractionOutput.model_json_schema()
    system_tiny = "You are an extraction agent. Reply with JSON."
    messages_tiny = [Message("user", "Return {\"entities\":[],\"rejected\":[],\"page_has_ownership_language\":false,\"agent_confidence\":0.5}")]
    test_with_json_transport(model, system_tiny, messages_tiny, 100, 12288, 60)
    
    print("\n" + "=" * 80)
    print("EXPERIMENT 2: Real extraction request (SHORT TIMEOUT)")
    print("=" * 80)
    
    print("\nReal extraction request with 30-second timeout:")
    test_with_requests(model, [{"role": "system", "content": system}, {"role": "user", "content": user}], options, 30, use_stream=True)
    test_with_httpx(model, [{"role": "system", "content": system}, {"role": "user", "content": user}], options, 30, use_stream=True)
    
    print("\nReal extraction request with 60-second timeout:")
    test_with_requests(model, [{"role": "system", "content": system}, {"role": "user", "content": user}], options, 60, use_stream=True)
    
    print("\nReal extraction request with 120-second timeout:")
    test_with_requests(model, [{"role": "system", "content": system}, {"role": "user", "content": user}], options, 120, use_stream=True)
    
    print("\nReal extraction request with JsonTransport (30s timeout):")
    test_with_json_transport(model, system, [Message("user", user)], options["num_predict"], options["num_ctx"], 30)
    
    print("\n" + "=" * 80)
    print("EXPERIMENT 3: Without format field")
    print("=" * 80)
    
    # Test without format field
    no_format_body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "options": options,
        "stream": False,
        "keep_alive": "30m",
    }
    
    print("\nWithout format, stream=False, 60s timeout:")
    t0 = time.perf_counter()
    try:
        r = requests.post("http://localhost:11434/api/chat", json=no_format_body, timeout=60)
        wall = time.perf_counter() - t0
        data = r.json()
        content = data.get("message", {}).get("content", "")
        print(f"  status={r.status_code}, time={wall:.1f}s, content={len(content)} chars")
    except Exception as exc:
        wall = time.perf_counter() - t0
        print(f"  {type(exc).__name__} after {wall:.1f}s")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
