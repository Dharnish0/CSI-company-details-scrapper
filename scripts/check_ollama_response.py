#!/usr/bin/env python
"""Check what Ollama actually sends."""

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import requests


def test_with_detailed_response(description, body, timeout):
    """Test and capture full response details."""
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
        print(f"  Headers: {dict(r.headers)}")
        
        # Check response body
        print(f"  Body length: {len(r.text)} bytes")
        print(f"  Body preview: {r.text[:500]}")
        
        if r.status_code == 200:
            data = json.loads(r.text)
            print(f"  Content: {data.get('message', {}).get('content', '')[:200]}")
            print(f"  Done reason: {data.get('done_reason')}")
        
        return True
        
    except requests.Timeout as exc:
        wall = time.perf_counter() - t0
        print(f"  TIMEOUT after {wall:.3f}s: {type(exc).__name__}")
        return False
    except Exception as exc:
        wall = time.perf_counter() - t0
        print(f"  {type(exc).__name__} after {wall:.3f}s")
        return False


def main():
    print("=" * 80)
    print("OLLAMA RESPONSE ANALYSIS")
    print("=" * 80)
    
    with open("diagnostics/extraction_raw_request.json") as f:
        real_req = json.load(f)
    
    model = real_req["model"]
    system = real_req["messages"][0]["content"]
    user = real_req["messages"][1]["content"]
    options = real_req["options"]
    
    # Build truncated requests
    for trunc_chars in [0, 100, 200, 500, 1000, 2000]:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user[:trunc_chars]} if trunc_chars > 0 else {"role": "user", "content": "OK"},
            ],
            "options": options,
            "stream": False,
            "keep_alive": "30m",
        }
        
        desc = f"User truncated to {trunc_chars} chars"
        test_with_detailed_response(desc, body, 60)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
