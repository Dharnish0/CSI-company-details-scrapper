#!/usr/bin/env python
"""Analyze the exact request body to identify what causes Ollama to timeout."""

import json
import pathlib
import time

import requests
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "mistral:latest"
TIMEOUT = 30

# Load real extraction request
with open("diagnostics/extraction_raw_request.json") as f:
    real_req = json.load(f)

print("=" * 80)
print("BODY COMPONENT ANALYSIS")
print("=" * 80)

# Extract components
system = real_req["messages"][0]["content"]
user = real_req["messages"][1]["content"]
options = real_req["options"]

print(f"System: {len(system)} chars")
print(f"User: {len(user)} chars")
print(f"Options: {options}")
print()

# Test each component
print("TEST 1: Just model and messages (minimal request)")
body = {
    "model": MODEL,
    "messages": [
        {"role": "system", "content": "Extract entities."},
        {"role": "user", "content": "Test"},
    ],
}
t0 = time.perf_counter()
try:
    r = requests.post(OLLAMA_URL, json=body, timeout=TIMEOUT, stream=True)
    wall = time.perf_counter() - t0
    print(f"  Result: {'SUCCESS' if r.status_code == 200 else f'HTTP {r.status_code}'} in {wall:.1f}s")
except requests.Timeout:
    wall = time.perf_counter() - t0
    print(f"  Result: TIMEOUT after {wall:.1f}s")

print()
print("TEST 2: Add num_predict")
body = {
    "model": MODEL,
    "messages": [
        {"role": "system", "content": "Extract entities."},
        {"role": "user", "content": "Test"},
    ],
    "options": {"num_predict": 100},
}
t0 = time.perf_counter()
try:
    r = requests.post(OLLAMA_URL, json=body, timeout=TIMEOUT, stream=True)
    wall = time.perf_counter() - t0
    print(f"  Result: {'SUCCESS' if r.status_code == 200 else f'HTTP {r.status_code}'} in {wall:.1f}s")
except requests.Timeout:
    wall = time.perf_counter() - t0
    print(f"  Result: TIMEOUT after {wall:.1f}s")

print()
print("TEST 3: Add num_ctx")
body = {
    "model": MODEL,
    "messages": [
        {"role": "system", "content": "Extract entities."},
        {"role": "user", "content": "Test"},
    ],
    "options": {"num_predict": 100, "num_ctx": 12288},
}
t0 = time.perf_counter()
try:
    r = requests.post(OLLAMA_URL, json=body, timeout=TIMEOUT, stream=True)
    wall = time.perf_counter() - t0
    print(f"  Result: {'SUCCESS' if r.status_code == 200 else f'HTTP {r.status_code}'} in {wall:.1f}s")
except requests.Timeout:
    wall = time.perf_counter() - t0
    print(f"  Result: TIMEOUT after {wall:.1f}s")

print()
print("TEST 4: Add temperature")
body = {
    "model": MODEL,
    "messages": [
        {"role": "system", "content": "Extract entities."},
        {"role": "user", "content": "Test"},
    ],
    "options": {"temperature": 0.0, "num_predict": 100, "num_ctx": 12288},
}
t0 = time.perf_counter()
try:
    r = requests.post(OLLAMA_URL, json=body, timeout=TIMEOUT, stream=True)
    wall = time.perf_counter() - t0
    print(f"  Result: {'SUCCESS' if r.status_code == 200 else f'HTTP {r.status_code}'} in {wall:.1f}s")
except requests.Timeout:
    wall = time.perf_counter() - t0
    print(f"  Result: TIMEOUT after {wall:.1f}s")

print()
print("TEST 5: Add stream")
body = {
    "model": MODEL,
    "stream": False,
    "messages": [
        {"role": "system", "content": "Extract entities."},
        {"role": "user", "content": "Test"},
    ],
    "options": {"temperature": 0.0, "num_predict": 100, "num_ctx": 12288},
}
t0 = time.perf_counter()
try:
    r = requests.post(OLLAMA_URL, json=body, timeout=TIMEOUT, stream=True)
    wall = time.perf_counter() - t0
    print(f"  Result: {'SUCCESS' if r.status_code == 200 else f'HTTP {r.status_code}'} in {wall:.1f}s")
except requests.Timeout:
    wall = time.perf_counter() - t0
    print(f"  Result: TIMEOUT after {wall:.1f}s")

print()
print("TEST 6: Add keep_alive")
body = {
    "model": MODEL,
    "stream": False,
    "keep_alive": "30m",
    "messages": [
        {"role": "system", "content": "Extract entities."},
        {"role": "user", "content": "Test"},
    ],
    "options": {"temperature": 0.0, "num_predict": 100, "num_ctx": 12288},
}
t0 = time.perf_counter()
try:
    r = requests.post(OLLAMA_URL, json=body, timeout=TIMEOUT, stream=True)
    wall = time.perf_counter() - t0
    print(f"  Result: {'SUCCESS' if r.status_code == 200 else f'HTTP {r.status_code}'} in {wall:.1f}s")
except requests.Timeout:
    wall = time.perf_counter() - t0
    print(f"  Result: TIMEOUT after {wall:.1f}s")

print()
print("TEST 7: Add think")
body = {
    "model": MODEL,
    "stream": False,
    "keep_alive": "30m",
    "think": False,
    "messages": [
        {"role": "system", "content": "Extract entities."},
        {"role": "user", "content": "Test"},
    ],
    "options": {"temperature": 0.0, "num_predict": 100, "num_ctx": 12288},
}
t0 = time.perf_counter()
try:
    r = requests.post(OLLAMA_URL, json=body, timeout=TIMEOUT, stream=True)
    wall = time.perf_counter() - t0
    print(f"  Result: {'SUCCESS' if r.status_code == 200 else f'HTTP {r.status_code}'} in {wall:.1f}s")
except requests.Timeout:
    wall = time.perf_counter() - t0
    print(f"  Result: TIMEOUT after {wall:.1f}s")

print()
print("TEST 8: Add format (simple schema)")
simple_schema = {
    "type": "object",
    "properties": {"test": {"type": "string"}},
    "required": ["test"],
}
body = {
    "model": MODEL,
    "stream": False,
    "keep_alive": "30m",
    "think": False,
    "format": simple_schema,
    "messages": [
        {"role": "system", "content": "Extract entities."},
        {"role": "user", "content": "Test"},
    ],
    "options": {"temperature": 0.0, "num_predict": 100, "num_ctx": 12288},
}
t0 = time.perf_counter()
try:
    r = requests.post(OLLAMA_URL, json=body, timeout=TIMEOUT, stream=True)
    wall = time.perf_counter() - t0
    print(f"  Result: {'SUCCESS' if r.status_code == 200 else f'HTTP {r.status_code}'} in {wall:.1f}s")
except requests.Timeout:
    wall = time.perf_counter() - t0
    print(f"  Result: TIMEOUT after {wall:.1f}s")

print()
print("TEST 9: Add format (full schema)")
full_schema = ExtractionOutput.model_json_schema()
from corpdisc.contracts.agent_io import ExtractionOutput

body = {
    "model": MODEL,
    "stream": False,
    "keep_alive": "30m",
    "think": False,
    "format": full_schema,
    "messages": [
        {"role": "system", "content": "Extract entities."},
        {"role": "user", "content": "Test"},
    ],
    "options": {"temperature": 0.0, "num_predict": 100, "num_ctx": 12288},
}
t0 = time.perf_counter()
try:
    r = requests.post(OLLAMA_URL, json=body, timeout=TIMEOUT, stream=True)
    wall = time.perf_counter() - t0
    print(f"  Result: {'SUCCESS' if r.status_code == 200 else f'HTTP {r.status_code}'} in {wall:.1f}s")
except requests.Timeout:
    wall = time.perf_counter() - t0
    print(f"  Result: TIMEOUT after {wall:.1f}s")
