#!/usr/bin/env python
"""Very narrow threshold test."""

import json
import time

import requests

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "mistral:latest"
TIMEOUT = 30

# Test between 1195 and 1250
for size in [1195, 1196, 1197, 1198, 1199, 1200, 1201, 1202, 1203, 1204, 1205, 1210, 1220, 1230, 1240, 1250]:
    base = "Extract entities. Use ONLY supplied data. "
    padding = "x" * (size - len(base))
    system = base + padding
    
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": '{"candidates":[],"page_text":"."}'}
        ],
        "options": {"temperature": 0.0, "num_predict": 100, "num_ctx": 12288},
        "stream": False,
        "keep_alive": "30m",
    }
    
    t0 = time.perf_counter()
    try:
        r = requests.post(OLLAMA_URL, json=body, timeout=TIMEOUT, stream=True)
        wall = time.perf_counter() - t0
        if r.status_code == 200:
            print(f"{size:5} chars: SUCCESS in {wall:.1f}s")
        else:
            print(f"{size:5} chars: HTTP {r.status_code} in {wall:.1f}s")
    except requests.Timeout:
        wall = time.perf_counter() - t0
        print(f"{size:5} chars: TIMEOUT in {wall:.1f}s")
