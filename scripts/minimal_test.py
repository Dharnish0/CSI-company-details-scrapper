#!/usr/bin/env python
"""Minimal test with various sizes."""

import json
import pathlib
import sys
import time

import requests


def test(body, label, timeout=120):
    """Test a request."""
    base_url = "http://localhost:11434"
    
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"{'='*60}")
    print(f"Request size: {len(json.dumps(body))} bytes")
    
    t0 = time.perf_counter()
    try:
        r = requests.post(f"{base_url}/api/chat", json=body, timeout=timeout, stream=True)
        wall = time.perf_counter() - t0
        
        if r.status_code != 200:
            print(f"HTTP {r.status_code} after {wall:.1f}s")
            return
        
        chunks = []
        for chunk in r.iter_content(chunk_size=65536):
            chunks.append(chunk)
        text = b"".join(chunks).decode("utf-8")
        data = json.loads(text)
        
        content = data.get("message", {}).get("content", "")
        print(f"Wall time: {wall:.1f}s")
        print(f"Content length: {len(content)} chars")
        print(f"Done reason: {data.get('done_reason')}")
        print(f"Prompt tok: {data.get('prompt_eval_count')}, Completion tok: {data.get('eval_count')}")
        
        if content:
            print(f"Content preview: {content[:200]}")
    except requests.Timeout:
        wall = time.perf_counter() - t0
        print(f"TIMEOUT after {wall:.1f}s")
    except Exception as e:
        wall = time.perf_counter() - t0
        print(f"Error after {wall:.1f}s: {type(e).__name__}: {str(e)[:100]}")


def main():
    print("=" * 60)
    print("MINIMAL OLLAMA TESTS")
    print("=" * 60)
    
    # Test 1: Tiny request
    test(
        {
            "model": "mistral:latest",
            "messages": [{"role": "user", "content": "OK"}],
            "options": {"temperature": 0.0, "num_predict": 10, "num_ctx": 12288},
            "stream": False,
            "keep_alive": "30m",
        },
        "TINY: 10 tokens",
    )
    
    # Test 2: With system prompt
    test(
        {
            "model": "mistral:latest",
            "messages": [
                {"role": "system", "content": "You are an extraction agent. Reply with JSON."},
                {"role": "user", "content": "Extract entities. Return JSON."},
            ],
            "options": {"temperature": 0.0, "num_predict": 100, "num_ctx": 12288},
            "stream": False,
            "keep_alive": "30m",
        },
        "SMALL: with system prompt",
    )
    
    # Test 3: Load the real request and reduce it
    with open("diagnostics/extraction_raw_request.json") as f:
        real_req = json.load(f)
    
    if "format" in real_req:
        del real_req["format"]
    
    # Reduce DOM candidates to 2
    user = json.loads(real_req["messages"][1]["content"])
    user["dom_candidates"] = user["dom_candidates"][:2]
    user["links"] = user["links"][:3]
    real_req["messages"][1]["content"] = json.dumps(user)
    
    test(real_req, "REDUCED: 2 DOM candidates")
    
    # Test 4: With 5 DOM candidates
    user["dom_candidates"] = user["dom_candidates"][:5]
    user["links"] = user["links"][:5]
    real_req["messages"][1]["content"] = json.dumps(user)
    
    test(real_req, "REDUCED: 5 DOM candidates")
    
    # Test 5: With 10 DOM candidates
    user["dom_candidates"] = user["dom_candidates"][:10]
    user["links"] = user["links"][:10]
    real_req["messages"][1]["content"] = json.dumps(user)
    
    test(real_req, "REDUCED: 10 DOM candidates")
    
    # Test 6: Original size (without format)
    with open("diagnostics/extraction_raw_request.json") as f:
        real_req = json.load(f)
    
    if "format" in real_req:
        del real_req["format"]
    
    test(real_req, "ORIGINAL: full extraction request")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
