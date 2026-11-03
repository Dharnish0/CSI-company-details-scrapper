#!/usr/bin/env python
"""Test with smaller context window."""
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import requests

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen3:4b"

# Load extraction request
with open(pathlib.Path(__file__).resolve().parent / "extraction_raw_request.json") as f:
    base_body = json.load(f)

from corpdisc.contracts.agent_io import ExtractionOutput
full_schema = ExtractionOutput.model_json_schema()

production_body = {
    **base_body,
    "format": full_schema,
    "options": {"temperature": 0.0, "num_predict": 1000, "num_ctx": 4096},  # Smaller context
}

print(f"Testing with smaller context (4096) and num_predict (1000)...")
t0 = time.perf_counter()
try:
    r = requests.post(OLLAMA_URL, json=production_body, timeout=60)
    total_time = time.perf_counter() - t0
    print(f"Status: {r.status_code}")
    print(f"Total time: {total_time:.3f}s")
    if r.status_code == 200:
        data = r.json()
        content = data.get("message", {}).get("content", "")
        print(f"Content length: {len(content)} chars")
        print(f"Done reason: {data.get('done_reason')}")
        print(f"Prompt eval count: {data.get('prompt_eval_count')}")
        print(f"Eval count: {data.get('eval_count')}")
        try:
            parsed = json.loads(content)
            print(f"JSON parsing: SUCCESS")
        except:
            print("JSON parsing: FAILED")
    else:
        print(f"Error: {r.text[:200]}")
except requests.Timeout:
    total_time = time.perf_counter() - t0
    print(f"Timeout after {total_time:.3f}s")
except Exception as e:
    total_time = time.perf_counter() - t0
    print(f"Error: {type(e).__name__}: {str(e)[:200]}")
