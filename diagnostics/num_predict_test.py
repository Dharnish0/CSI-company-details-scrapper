#!/usr/bin/env python
"""Test num_predict specifically."""

import json
import time

import requests

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "mistral:latest"
TIMEOUT = 30

for num_predict in [5, 10, 20, 50, 100, 200, 500, 1000, 2000, 3000]:
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "Extract entities."},
            {"role": "user", "content": "Test"},
        ],
        "options": {"temperature": 0.0, "num_predict": num_predict, "num_ctx": 12288},
    }
    
    print(f"num_predict={num_predict:4}: ", end="")
    t0 = time.perf_counter()
    try:
        r = requests.post(OLLAMA_URL, json=body, timeout=TIMEOUT, stream=True)
        wall = time.perf_counter() - t0
        
        if r.status_code == 200:
            print(f"SUCCESS in {wall:.1f}s")
        else:
            print(f"HTTP {r.status_code} in {wall:.1f}s")
    except requests.Timeout:
        wall = time.perf_counter() - t0
        print(f"TIMEOUT after {wall:.1f}s")
    except Exception as e:
        wall = time.perf_counter() - t0
        print(f"{type(e).__name__} after {wall:.1f}s")
