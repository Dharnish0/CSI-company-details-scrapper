#!/usr/bin/env python
"""Simple transport test without stream."""

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import requests
import httpx


def test(description, body, timeout, use_stream=False):
    """Test a request."""
    base_url = "http://localhost:11434"
    
    print(f"\n{description}")
    print(f"  timeout={timeout}s, stream={use_stream}: ", end="")
    
    t0 = time.perf_counter()
    try:
        r = requests.post(
            f"{base_url}/api/chat",
            json=body,
            timeout=timeout,
            stream=use_stream,
        )
        wall = time.perf_counter() - t0
        
        if use_stream:
            chunks = []
            for chunk in r.iter_content(chunk_size=65536):
                chunks.append(chunk)
            text = b"".join(chunks).decode("utf-8")
        else:
            text = r.text
        
        data = json.loads(text)
        content = data.get("message", {}).get("content", "")
        
        print(f"OK! time={wall:.1f}s, status={r.status_code}, content={len(content)} chars")
        return True
        
    except requests.Timeout:
        wall = time.perf_counter() - t0
        print(f"TIMEOUT after {wall:.1f}s")
        return False
    except json.JSONDecodeError as exc:
        wall = time.perf_counter() - t0
        print(f"JSONDecodeError after {wall:.1f}s")
        return False
    except Exception as exc:
        wall = time.perf_counter() - t0
        print(f"{type(exc).__name__} after {wall:.1f}s: {str(exc)[:100]}")
        return False


def main():
    print("=" * 80)
    print("SIMPLE TRANSPORT TEST")
    print("=" * 80)
    
    with open("diagnostics/extraction_raw_request.json") as f:
        real_req = json.load(f)
    
    model = real_req["model"]
    system = real_req["messages"][0]["content"]
    user = real_req["messages"][1]["content"]
    options = real_req["options"]
    
    print(f"\nReal extraction request:")
    print(f"  System: {len(system)} chars")
    print(f"  User: {len(user)} chars")
    print(f"  Total: {len(json.dumps(real_req))} bytes")
    
    # Test 1: Tiny request, no stream
    tiny_body = {
        "model": model,
        "messages": [{"role": "user", "content": "OK"}],
        "options": {"temperature": 0.0, "num_predict": 10, "num_ctx": 12288},
        "stream": False,
        "keep_alive": "30m",
    }
    
    test("Tiny request (stream=False)", tiny_body, 60, use_stream=False)
    
    # Test 2: Small system prompt, no stream
    small_body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are an agent. Reply with JSON."},
            {"role": "user", "content": user[:500]},
        ],
        "options": options,
        "stream": False,
        "keep_alive": "30m",
    }
    
    test("Small prompt (500 chars, stream=False)", small_body, 60, use_stream=False)
    
    # Test 3: Real system prompt, truncated user
    truncated_body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user[:1000]},
        ],
        "options": options,
        "stream": False,
        "keep_alive": "30m",
    }
    
    test("Real system + truncated user (1000 chars, stream=False)", truncated_body, 60, use_stream=False)
    
    # Test 4: Real system prompt, real user
    real_body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": options,
        "stream": False,
        "keep_alive": "30m",
    }
    
    print(f"\n{'='*80}")
    print("CRITICAL TEST: Real extraction with stream=False, 120s timeout")
    print(f"{'='*80}")
    test("Real extraction (stream=False, 120s)", real_body, 120, use_stream=False)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
