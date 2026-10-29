#!/usr/bin/env python
"""Test whether Ollama sends ANY response at all."""

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import requests


def test(description, body, timeout):
    """Test with stream=False - does Ollama send ANY response?"""
    base_url = "http://localhost:11434"
    
    print(f"\n{description}")
    print(f"  Timeout: {timeout}s")
    
    t0 = time.perf_counter()
    
    try:
        r = requests.post(
            f"{base_url}/api/chat",
            json=body,
            timeout=timeout,
            stream=False,
        )
        
        wall = time.perf_counter() - t0
        print(f"  Response received at: {wall:.3f}s")
        print(f"  Status: {r.status_code}")
        
        if r.status_code == 200:
            print(f"  Body length: {len(r.text)} bytes")
            data = json.loads(r.text)
            content = data.get('message', {}).get('content', '')
            print(f"  Content length: {len(content)} chars")
            print(f"  Done reason: {data.get('done_reason')}")
            return True
        else:
            print(f"  Body preview: {r.text[:200]}")
            return False
            
    except requests.Timeout as exc:
        wall = time.perf_counter() - t0
        print(f"  TIMEOUT after {wall:.3f}s - NO RESPONSE RECEIVED")
        return False
    except Exception as exc:
        wall = time.perf_counter() - t0
        print(f"  {type(exc).__name__} after {wall:.3f}s")
        return False


def main():
    print("=" * 80)
    print("NO RESPONSE TEST - Does Ollama send ANY response?")
    print("=" * 80)
    
    # Load the real request
    with open("diagnostics/extraction_raw_request.json") as f:
        real_req = json.load(f)
    
    model = real_req["model"]
    system = real_req["messages"][0]["content"]
    user = real_req["messages"][1]["content"]
    options = real_req["options"]
    
    print(f"\nReal extraction request:")
    print(f"  System prompt: {len(system)} chars")
    print(f"  User message: {len(user)} chars")
    
    # Test with various user truncations
    for trunc in [0, 50, 100, 200, 500, 1000]:
        user_part = user[:trunc] if trunc > 0 else "OK"
        
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_part},
            ],
            "options": options,
            "stream": False,
            "keep_alive": "30m",
        }
        
        test(f"User: {trunc} chars (30s timeout)", body, 30)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
