#!/usr/bin/env python
"""Test qwen3:4b simple request directly."""
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import requests

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen3:4b"

# Simple test
control_body = {
    "model": MODEL,
    "messages": [{"role": "user", "content": "Return the word OK"}],
    "options": {"temperature": 0.0, "num_predict": 20, "num_ctx": 12288},
    "stream": False,
    "keep_alive": "30m",
}

print("Testing simple request...")
r = requests.post(OLLAMA_URL, json=control_body, timeout=30)
print(f"Status: {r.status_code}")
print(f"Response (first 500 chars): {r.text[:500]}")
data = r.json()
content = data.get("message", {}).get("content", "")
print(f"Message content: {repr(content)}")
print(f"Done reason: {data.get('done_reason')}")
print(f"Done: {data.get('done')}")

# Now test the extraction request directly
print("\nTesting extraction request...")
with open(pathlib.Path(__file__).resolve().parent / "extraction_raw_request.json") as f:
    base_body = json.load(f)

from corpdisc.contracts.agent_io import ExtractionOutput
full_schema = ExtractionOutput.model_json_schema()

production_body = {
    **base_body,
    "format": full_schema,
}

t0 = time.perf_counter()
try:
    r = requests.post(OLLAMA_URL, json=production_body, timeout=60)
    total_time = time.perf_counter() - t0
    print(f"Status: {r.status_code}")
    print(f"Total time: {total_time:.3f}s")
    if r.status_code == 200:
        data = r.json()
        content = data.get("message", {}).get("content", "")
        print(f"Response content (first 500 chars): {content[:500]}")
        print(f"Content length: {len(content)} chars")
        print(f"Done reason: {data.get('done_reason')}")
        print(f"Done: {data.get('done')}")
        print(f"Prompt eval count: {data.get('prompt_eval_count')}")
        print(f"Eval count: {data.get('eval_count')}")
        # Try to parse as JSON
        try:
            parsed = json.loads(content)
            print(f"JSON parsing: SUCCESS")
            print(f"Has entities: {'entities' in parsed}")
            print(f"Has rejected: {'rejected' in parsed}")
        except json.JSONDecodeError as e:
            print(f"JSON parsing: FAILED - {e}")
    else:
        print(f"Error response: {r.text[:500]}")
except requests.Timeout:
    total_time = time.perf_counter() - t0
    print(f"Timeout after {total_time:.3f}s")
except Exception as e:
    total_time = time.perf_counter() - t0
    print(f"Error: {type(e).__name__}: {str(e)[:200]}")
    print(f"Time: {total_time:.3f}s")
