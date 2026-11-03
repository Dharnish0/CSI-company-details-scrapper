#!/usr/bin/env python
"""Test whether specific user content causes the issue."""

import json
import pathlib
import time

import requests
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "mistral:latest"
TIMEOUT = 30

# Base system prompt (short)
system = "Extract entities. Return JSON."

# Test various user content sizes
user_contents = {
    "tiny": '{"test": true}',
    "small": '{"candidates": [{"id": 1, "name": "Test"}], "page_text": "Test."}',
    "medium": '{"candidates": [{"id": 1, "name": "Test"}, {"id": 2, "name": "Test2"}], "page_text": "Test content that is a bit longer."}',
    "large": '{"candidates": [{"id": 1, "name": "Test1"}, {"id": 2, "name": "Test2"}, {"id": 3, "name": "Test3"}], "page_text": "Test content that is a bit longer with more details."}',
}

# Real extraction user content
with open("diagnostics/extraction_raw_request.json") as f:
    real_req = json.load(f)
real_user = real_req["messages"][1]["content"]

print("=" * 80)
print("USER CONTENT TEST")
print("=" * 80)

for name, content in user_contents.items():
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
        "options": {"temperature": 0.0, "num_predict": 100, "num_ctx": 12288},
        "stream": False,
        "keep_alive": "30m",
    }
    
    print(f"{name} ({len(content)} chars):", end=" ")
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
    except Exception as exc:
        wall = time.perf_counter() - t0
        print(f"{type(exc).__name__} after {wall:.1f}s")

# Test with real extraction user content (but short system prompt)
print(f"\nReal extraction user content ({len(real_user)} chars) with short system prompt:")
body = {
    "model": MODEL,
    "messages": [
        {"role": "system", "content": "Extract entities. Return JSON."},
        {"role": "user", "content": real_user},
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
        print(f"SUCCESS in {wall:.1f}s")
    else:
        print(f"HTTP {r.status_code} in {wall:.1f}s")
except requests.Timeout:
    wall = time.perf_counter() - t0
    print(f"TIMEOUT after {wall:.1f}s")
except Exception as exc:
    wall = time.perf_counter() - t0
    print(f"{type(exc).__name__} after {wall:.1f}s")
