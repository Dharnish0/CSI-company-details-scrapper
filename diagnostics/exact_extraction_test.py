#!/usr/bin/env python
"""Test the EXACT extraction request."""

import json
import pathlib
import time

import requests
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from corpdisc.agents.prompts import EXTRACTION_V1
from corpdisc.contracts.agent_io import ExtractionOutput

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "mistral:latest"
TIMEOUT = 30

# Load real user content
with open("diagnostics/extraction_raw_request.json") as f:
    real_req = json.load(f)

system = real_req["messages"][0]["content"]
user = real_req["messages"][1]["content"]

print("=" * 80)
print("EXACT EXTRACTION REQUEST TEST")
print("=" * 80)
print(f"System prompt: {len(system)} chars, {len(system.encode('utf-8'))} bytes")
print(f"User message: {len(user)} chars, {len(user.encode('utf-8'))} bytes")
print(f"Total body: {len(json.dumps(real_req))} bytes")
print()

body = {**real_req, "stream": False}

print(f"Testing with {TIMEOUT}s timeout...")
t0 = time.perf_counter()
try:
    r = requests.post(OLLAMA_URL, json=body, timeout=TIMEOUT, stream=True)
    wall = time.perf_counter() - t0
    
    if r.status_code == 200:
        chunks = []
        for chunk in r.iter_content(chunk_size=65536):
            chunks.append(chunk)
        text = b"".join(chunks).decode("utf-8")
        data = json.loads(text)
        content = data.get("message", {}).get("content", "")
        
        print(f"✅ SUCCESS: {wall:.1f}s, content={len(content)} chars")
        print(f"Response preview: {content[:300]}")
    else:
        print(f"HTTP {r.status_code} after {wall:.1f}s")
        
except requests.Timeout:
    wall = time.perf_counter() - t0
    print(f"❌ TIMEOUT after {wall:.1f}s")
except Exception as exc:
    wall = time.perf_counter() - t0
    print(f"❌ {type(exc).__name__} after {wall:.1f}s")

print()
print("=" * 80)
print("TESTING WITH TRUNCATED SYSTEM PROMPT")
print("=" * 80)

# Try truncating system prompt to various sizes
for trunc in [1000, 900, 800, 700, 600, 500, 400]:
    truncated_system = system[:trunc]
    
    truncated_body = {
        **real_req,
        "messages": [
            {"role": "system", "content": truncated_system},
            {"role": "user", "content": user},
        ],
        "stream": False,
    }
    
    print(f"Truncated to {trunc} chars:", end=" ")
    t0 = time.perf_counter()
    try:
        r = requests.post(OLLAMA_URL, json=truncated_body, timeout=TIMEOUT, stream=True)
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
