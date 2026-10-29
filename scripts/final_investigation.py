#!/usr/bin/env python
"""Final investigation - exact timeout location."""

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import requests


def test_with_precise_timing(description, body, timeout):
    """Test with precise timing and response capture."""
    base_url = "http://localhost:11434"
    
    print(f"\n{description}")
    print(f"  Timeout: {timeout}s")
    
    t0 = time.perf_counter()
    
    try:
        r = requests.post(
            f"{base_url}/api/chat",
            json=body,
            timeout=timeout,
            stream=True,
        )
        
        t_after_request = time.perf_counter()
        print(f"  Request sent at: {t_after_request - t0:.3f}s")
        print(f"  HTTP status: {r.status_code}")
        
        # Try to read response with very short read timeout
        # The key is whether we get ANY bytes
        chunks = []
        try:
            # Use a very short timeout for reading individual chunks
            for chunk in r.iter_content(chunk_size=1024):
                chunks.append(chunk)
                if len(chunks) <= 3:
                    print(f"  Received chunk #{len(chunks)} at {time.perf_counter() - t0:.3f}s (size: {len(chunk)})")
        
        except requests.exceptions.Timeout as exc:
            print(f"  Chunk read timeout at {time.perf_counter() - t0:.3f}s")
        
        t_complete = time.perf_counter()
        print(f"  Complete at: {t_complete - t0:.3f}s")
        
        if chunks:
            text = b"".join(chunks).decode("utf-8", errors="replace")
            print(f"  Total bytes received: {len(text)}")
            print(f"  First 200 chars: {text[:200]}")
        
        return True
        
    except requests.Timeout as exc:
        wall = time.perf_counter() - t0
        print(f"  TIMEOUT after {wall:.3f}s")
        return False
    except Exception as exc:
        wall = time.perf_counter() - t0
        print(f"  {type(exc).__name__} after {wall:.3f}s: {str(exc)[:100]}")
        return False


def main():
    print("=" * 80)
    print("FINAL INVESTIGATION - Exact Timeout Location")
    print("=" * 80)
    
    # Test 1: Very simple request with 5s timeout
    print("\n" + "=" * 80)
    print("TEST 1: Very simple request (5s timeout)")
    print("=" * 80)
    
    body1 = {
        "model": "mistral:latest",
        "messages": [{"role": "user", "content": "OK"}],
        "options": {"temperature": 0.0, "num_predict": 10, "num_ctx": 12288},
        "stream": True,
        "keep_alive": "30m",
    }
    
    test_with_precise_timing("Simple request (5s)", body1, 5)
    
    # Test 2: Same but with 10s timeout
    print("\n" + "=" * 80)
    print("TEST 2: Same request (10s timeout)")
    print("=" * 80)
    
    test_with_precise_timing("Simple request (10s)", body1, 10)
    
    # Test 3: With system prompt
    print("\n" + "=" * 80)
    print("TEST 3: With system prompt (10s timeout)")
    print("=" * 80)
    
    body2 = {
        "model": "mistral:latest",
        "messages": [
            {"role": "system", "content": "You are an agent. Reply with JSON."},
            {"role": "user", "content": "OK"},
        ],
        "options": {"temperature": 0.0, "num_predict": 10, "num_ctx": 12288},
        "stream": True,
        "keep_alive": "30m",
    }
    
    test_with_precise_timing("With system prompt (10s)", body2, 10)
    
    # Test 4: Load the real request and test with 5s timeout
    print("\n" + "=" * 80)
    print("TEST 4: Real request (5s timeout)")
    print("=" * 80)
    
    with open("diagnostics/extraction_raw_request.json") as f:
        real_req = json.load(f)
    
    real_req['stream'] = True
    test_with_precise_timing("Real extraction (5s)", real_req, 5)
    
    # Test 5: Real request with 10s timeout
    print("\n" + "=" * 80)
    print("TEST 5: Real request (10s timeout)")
    print("=" * 80)
    
    test_with_precise_timing("Real extraction (10s)", real_req, 10)
    
    # Test 6: Real request with 15s timeout
    print("\n" + "=" * 80)
    print("TEST 6: Real request (15s timeout)")
    print("=" * 80)
    
    test_with_precise_timing("Real extraction (15s)", real_req, 15)
    
    # Test 7: Real request with 20s timeout
    print("\n" + "=" * 80)
    print("TEST 7: Real request (20s timeout)")
    print("=" * 80)
    
    test_with_precise_timing("Real extraction (20s)", real_req, 20)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
