#!/usr/bin/env python
"""Direct model test."""

import json
import time

import requests

OLLAMA_URL = "http://localhost:11434/api/chat"

body = {
    "model": "mistral:latest",
    "messages": [{"role": "user", "content": "OK"}],
    "options": {"temperature": 0.0, "num_predict": 5, "num_ctx": 12288},
    "stream": False,
    "keep_alive": "30m",
}

for i in range(3):
    t0 = time.perf_counter()
    try:
        r = requests.post(OLLAMA_URL, json=body, timeout=60)
        wall = time.perf_counter() - t0
        print(f"Test {i+1}: status={r.status_code}, time={wall:.1f}s")
        if r.status_code == 200:
            data = r.json()
            content = data.get("message", {}).get("content", "")
            print(f"  Response: {content[:50]}")
    except Exception as e:
        print(f"Test {i+1}: ERROR {type(e).__name__}: {str(e)[:100]}")
