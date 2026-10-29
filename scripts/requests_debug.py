#!/usr/bin/env python
"""Debug requests library timeout behavior."""

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import requests


def test(description, body, timeout, use_stream=True):
    """Test a request with detailed timing."""
    base_url = "http://localhost:11434"
    
    print(f"\n{description}")
    print(f"  Body size: {len(json.dumps(body))} bytes")
    print(f"  Timeout: {timeout}s, stream={use_stream}")
    
    t0 = time.perf_counter()
    try:
        r = requests.post(
            f"{base_url}/api/chat",
            json=body,
            timeout=timeout,
            stream=use_stream,
        )
        
        # Record time to first header
        t_header = time.perf_counter()
        print(f"  Headers received at: {t_header - t0:.3f}s")
        
        if use_stream:
            # Try to get first chunk
            try:
                chunk = next(r.iter_content(chunk_size=65536))
                t_first_chunk = time.perf_counter()
                print(f"  First chunk received at: {t_first_chunk - t0:.3f}s")
                
                # Continue reading
                chunks = [chunk]
                for chunk in r.iter_content(chunk_size=65536):
                    chunks.append(chunk)
                text = b"".join(chunks).decode("utf-8")
                t_complete = time.perf_counter()
                print(f"  Complete at: {t_complete - t0:.3f}s")
            except StopIteration:
                print("  No chunks received")
                text = ""
        else:
            text = r.text
            t_complete = time.perf_counter()
            print(f"  Complete at: {t_complete - t0:.3f}s")
        
        wall = time.perf_counter() - t0
        data = json.loads(text)
        content = data.get("message", {}).get("content", "")
        
        print(f"  SUCCESS! Status={r.status_code}, content={len(content)} chars")
        return True
        
    except requests.Timeout as exc:
        wall = time.perf_counter() - t0
        print(f"  TIMEOUT after {wall:.3f}s: {type(exc).__name__}")
        return False
    except json.JSONDecodeError as exc:
        wall = time.perf_counter() - t0
        print(f"  JSONDecodeError after {wall:.3f}s")
        return False
    except Exception as exc:
        wall = time.perf_counter() - t0
        print(f"  {type(exc).__name__} after {wall:.3f}s: {str(exc)[:100]}")
        return False


def main():
    print("=" * 80)
    print("REQUESTS LIBRARY DEBUG - Timeout Behavior")
    print("=" * 80)
    
    with open("diagnostics/extraction_raw_request.json") as f:
        real_req = json.load(f)
    
    model = real_req["model"]
    system = real_req["messages"][0]["content"]
    user = real_req["messages"][1]["content"]
    options = real_req["options"]
    
    # Test 1: Tiny request
    tiny_body = {
        "model": model,
        "messages": [{"role": "user", "content": "OK"}],
        "options": {"temperature": 0.0, "num_predict": 10, "num_ctx": 12288},
        "stream": False,
        "keep_alive": "30m",
    }
    
    test("Tiny request (stream=False)", tiny_body, 60, use_stream=False)
    
    # Test 2: Small system prompt, small user
    small_body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are an agent."},
            {"role": "user", "content": "Return OK"},
        ],
        "options": options,
        "stream": False,
        "keep_alive": "30m",
    }
    
    test("Small prompt (stream=False)", small_body, 60, use_stream=False)
    
    # Test 3: Real system, small user
    real_system_small_user = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "Return OK"},
        ],
        "options": options,
        "stream": False,
        "keep_alive": "30m",
    }
    
    test("Real system + small user (stream=False)", real_system_small_user, 60, use_stream=False)
    
    # Test 4: Real system, truncated user (500 chars)
    real_system_trunc_user = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user[:500]},
        ],
        "options": options,
        "stream": False,
        "keep_alive": "30m",
    }
    
    test("Real system + 500 chars user (stream=False)", real_system_trunc_user, 60, use_stream=False)
    
    # Test 5: Real system, 1000 chars user
    real_system_1000_user = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user[:1000]},
        ],
        "options": options,
        "stream": False,
        "keep_alive": "30m",
    }
    
    test("Real system + 1000 chars user (stream=False)", real_system_1000_user, 60, use_stream=False)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
