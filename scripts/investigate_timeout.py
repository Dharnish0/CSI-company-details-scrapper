#!/usr/bin/env python
"""Investigate the exact timeout cause with incremental experiments."""

import json
import pathlib
import sys
import time

import requests


def test_request(description: str, body: dict, timeout: float = 300) -> dict:
    """Test a request and record results."""
    base_url = "http://localhost:11434"
    
    print(f"\n{'='*80}")
    print(f"TEST: {description}")
    print(f"{'='*80}")
    print(f"Timeout: {timeout}s")
    
    t0 = time.perf_counter()
    try:
        r = requests.post(f"{base_url}/api/chat", json=body, timeout=timeout, stream=True)
        wall = time.perf_counter() - t0
        
        chunks = []
        for chunk in r.iter_content(chunk_size=65536):
            chunks.append(chunk)
        text = b''.join(chunks).decode('utf-8')
        data = json.loads(text)
        
        content = data.get("message", {}).get("content", "")
        return {
            "description": description,
            "wall_s": round(wall, 1),
            "status": "OK",
            "status_code": r.status_code,
            "content_length": len(content),
            "done_reason": data.get("done_reason"),
            "metrics": {
                "prompt_tokens": data.get("prompt_eval_count", 0),
                "completion_tokens": data.get("eval_count", 0),
                "total_ms": round(data.get("total_duration", 0) / 1e6, 1),
                "load_ms": round(data.get("load_duration", 0) / 1e6, 1),
                "prompt_eval_ms": round(data.get("prompt_eval_duration", 0) / 1e6, 1),
                "eval_ms": round(data.get("eval_duration", 0) / 1e6, 1),
            },
        }
    except requests.Timeout:
        wall = time.perf_counter() - t0
        return {
            "description": description,
            "wall_s": round(wall, 1),
            "status": "TIMEOUT",
            "status_code": None,
        }
    except Exception as e:
        wall = time.perf_counter() - t0
        return {
            "description": description,
            "wall_s": round(wall, 1),
            "status": type(e).__name__,
            "status_code": None,
        }


def main():
    # Load the real extraction request
    raw_path = pathlib.Path(__file__).resolve().parent.parent / "diagnostics" / "extraction_raw_request.json"
    with open(raw_path) as f:
        real_req = json.load(f)
    
    # Load the schema
    schema_path = pathlib.Path(__file__).resolve().parent.parent / "diagnostics" / "extraction_request.json"
    with open(schema_path) as f:
        req_info = json.load(f)
    
    print("=" * 80)
    print("TIMEOUT INVESTIGATION - Incremental Experiments")
    print("=" * 80)
    print(f"Base URL: http://localhost:11434")
    print(f"Real request size: {len(json.dumps(real_req))} bytes")
    print(f"System prompt: {len(real_req['messages'][0]['content'])} chars")
    print(f"User message: {len(real_req['messages'][1]['content'])} chars")
    print(f"Schema: {req_info['response_schema_name']} ({req_info['response_schema_size']} bytes)")
    
    results = []
    
    # Base request (the failing one)
    body_base = {k: v for k, v in real_req.items() if k != "format"}
    results.append(test_request("BASE: Full extraction request", body_base, timeout=300))
    
    # Test with reduced num_ctx
    body_4096 = {**body_base, "options": {**body_base["options"], "num_ctx": 4096}}
    results.append(test_request("REDUCED_CTX_4096", body_4096, timeout=300))
    
    # Test with reduced num_predict
    body_1000 = {**body_base, "options": {**body_base["options"], "num_predict": 1000}}
    results.append(test_request("REDUCED_OUTPUT_1000", body_1000, timeout=300))
    
    # Test with simpler user message (remove most DOM candidates)
    body_simple = {**body_base}
    user = json.loads(body_simple["messages"][1]["content"])
    user["dom_candidates"] = user["dom_candidates"][:3]  # Only 3 candidates
    body_simple["messages"][1]["content"] = json.dumps(user)
    results.append(test_request("SIMPLER_USER_MESSAGE", body_simple, timeout=300))
    
    # Test with minimal system prompt
    body_minimal = {
        "model": real_req["model"],
        "stream": False,
        "keep_alive": "30m",
        "options": body_base["options"],
        "messages": [
            {"role": "system", "content": "Extract entities. Return JSON."},
            {"role": "user", "content": real_req["messages"][1]["content"][:500]},  # Truncate user
        ],
    }
    results.append(test_request("MINIMAL_SYSTEM_PROMPT", body_minimal, timeout=300))
    
    # Test with simple schema in format
    simple_schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
    body_with_simple_format = {**body_base, "format": simple_schema}
    results.append(test_request("WITH_SIMPLE_FORMAT", body_with_simple_format, timeout=300))
    
    # Test with full schema in format
    body_with_full_format = {**body_base, "format": req_info["response_schema"]}
    results.append(test_request("WITH_FULL_FORMAT_SCHEMA", body_with_full_format, timeout=300))
    
    # Summary
    print("\n" + "=" * 80)
    print("EXPERIMENT SUMMARY")
    print("=" * 80)
    print(f"{'Description':<40} | {'Wall (s)':<8} | {'Status':<10} | {'Content':<10}")
    print("-" * 80)
    
    for r in results:
        status = r["status"]
        if status == "OK":
            status_str = f"OK ({r['status_code']})"
            content = f"{r['content_length']}B"
        else:
            status_str = status
            content = "-"
        
        print(f"{r['description']:<40} | {r['wall_s']:>6.1f}s | {status_str:<10} | {content:<10}")
        
        if status == "OK" and "metrics" in r:
            m = r["metrics"]
            print(f"  -> prompt={m['prompt_tokens']} tok, completion={m['completion_tokens']} tok")
            print(f"  -> total={m['total_ms']}ms, eval={m['eval_ms']}ms")
    
    # Save results
    results_path = pathlib.Path(__file__).resolve().parent.parent / "diagnostics" / "timeout_investigation.json"
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {results_path}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
