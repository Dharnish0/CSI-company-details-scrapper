#!/usr/bin/env python
"""Exact threshold test - find the precise size where Ollama stops responding."""

from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import requests

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "mistral:latest"
TIMEOUT = 30

def make_request(system_content_length, user_content):
    """Create a request with specific system prompt length."""
    # Generate system prompt of exact length
    base_prompt = "Extract entities. Use ONLY supplied data. "
    padding = "x" * (system_content_length - len(base_prompt))
    system_prompt = base_prompt + padding
    
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "options": {
            "temperature": 0.0,
            "num_predict": 100,
            "num_ctx": 12288,
        },
        "stream": False,
        "keep_alive": "30m",
    }
    
    return body, len(system_prompt)


def test_threshold(target_size, user_content):
    """Test at a specific target size."""
    body, prompt_len = make_request(target_size, user_content)
    request_bytes = len(json.dumps(body).encode("utf-8"))
    
    print(f"  Target: {target_size} chars, Actual: {prompt_len} chars, Request: {request_bytes} bytes")
    
    t0 = time.perf_counter()
    try:
        r = requests.post(OLLAMA_URL, json=body, timeout=TIMEOUT, stream=True)
        wall = time.perf_counter() - t0
        
        if r.status_code == 200:
            chunks = []
            for chunk in r.iter_content(chunk_size=65536):
                chunks.append(chunk)
            text = b"".join(chunks).decode("utf-8")
            data = json.loads(text)
            content = data.get("message", {}).get("content", "")
            
            print(f"    ✅ SUCCESS: {wall:.1f}s, content={len(content)} chars")
            return "SUCCESS"
        else:
            print(f"    HTTP {r.status_code} after {wall:.1f}s")
            return f"HTTP {r.status_code}"
            
    except requests.Timeout:
        wall = time.perf_counter() - t0
        print(f"    ❌ TIMEOUT after {wall:.1f}s")
        return "TIMEOUT"
    except Exception as exc:
        wall = time.perf_counter() - t0
        print(f"    ❌ {type(exc).__name__} after {wall:.1f}s")
        return type(exc).__name__


def main():
    print("=" * 80)
    print("EXACT THRESHOLD TEST")
    print("=" * 80)
    
    # Fixed user content
    user_content = '{"candidates":[{"id":1,"name":"Test Company"}],"page_text":"Test content."}'
    
    print("\nTesting various prompt sizes:")
    
    results = []
    
    # Test at intervals
    for size in [100, 200, 300, 500, 800, 1000, 1200, 1400, 1600, 1800, 2000, 2500, 3000, 3500, 4000, 4500, 5000]:
        result = test_threshold(size, user_content)
        results.append({"size": size, "result": result})
    
    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"{'Prompt Size':<14} | {'Result':<12}")
    print("-" * 46)
    
    for r in results:
        print(f"{r['size']:<14} | {r['result']:<12}")
    
    # Find the threshold
    success_sizes = [r["size"] for r in results if r["result"] == "SUCCESS"]
    failure_sizes = [r["size"] for r in results if r["result"] != "SUCCESS"]
    
    print(f"\nLargest successful prompt: {max(success_sizes) if success_sizes else 'N/A'}")
    print(f"Smallest failing prompt: {min(failure_sizes) if failure_sizes else 'N/A'}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
