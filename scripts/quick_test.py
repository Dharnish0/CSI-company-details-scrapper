#!/usr/bin/env python
"""Quick test with simple request."""

import json
import time

import requests

base_url = "http://localhost:11434"

# Test 1: Simple request
print("Test 1: Simple plain text")
body = {
    "model": "mistral:latest",
    "messages": [{"role": "user", "content": "Return OK"}],
    "options": {"temperature": 0.0, "num_predict": 10, "num_ctx": 12288},
    "stream": False,
}

t0 = time.perf_counter()
resp = requests.post(f"{base_url}/api/chat", json=body, timeout=30)
wall = time.perf_counter() - t0
print(f"  Wall time: {wall:.1f}s, Status: {resp.status_code}")
print(f"  Response: {resp.json()}")

# Test 2: Simple JSON without format
print("\nTest 2: Simple JSON without format")
body = {
    "model": "mistral:latest",
    "messages": [
        {"role": "system", "content": "Return JSON with answer field"},
        {"role": "user", "content": "Answer: test"}
    ],
    "options": {"temperature": 0.0, "num_predict": 100, "num_ctx": 12288},
    "stream": False,
}

t0 = time.perf_counter()
resp = requests.post(f"{base_url}/api/chat", json=body, timeout=30)
wall = time.perf_counter() - t0
print(f"  Wall time: {wall:.1f}s, Status: {resp.status_code}")
data = resp.json()
content = data.get("message", {}).get("content", "")
print(f"  Response length: {len(content)} chars")
print(f"  Response preview: {content[:200]}")

# Test 3: Simple JSON with format
print("\nTest 3: Simple JSON with format (simple schema)")
simple_schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
body = {
    "model": "mistral:latest",
    "messages": [
        {"role": "system", "content": "Return JSON matching schema"},
        {"role": "user", "content": "Answer: test"}
    ],
    "options": {"temperature": 0.0, "num_predict": 100, "num_ctx": 12288},
    "stream": False,
    "format": simple_schema,
}

t0 = time.perf_counter()
resp = requests.post(f"{base_url}/api/chat", json=body, timeout=30)
wall = time.perf_counter() - t0
print(f"  Wall time: {wall:.1f}s, Status: {resp.status_code}")
data = resp.json()
content = data.get("message", {}).get("content", "")
print(f"  Response length: {len(content)} chars")
print(f"  Response: {content}")

print("\nAll quick tests completed successfully!")
