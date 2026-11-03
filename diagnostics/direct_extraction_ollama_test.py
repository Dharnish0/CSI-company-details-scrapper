#!/usr/bin/env python
"""Direct Ollama test - isolate Ollama vs provider issue."""

from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import requests

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen3:4b"
TIMEOUT = 60  # Short diagnostic timeout

# Load the production request (this is WITHOUT format, as it was saved earlier)
# We need to ADD the format field to match production
with open(pathlib.Path(__file__).resolve().parent / "extraction_raw_request.json") as f:
    base_body = json.load(f)

# Load the full schema
from corpdisc.contracts.agent_io import ExtractionOutput
full_schema = ExtractionOutput.model_json_schema()

# Build the PRODUCTION request with format field
production_body = {
    **base_body,
    "format": full_schema,
    "stream": False,  # The provider uses stream=True internally
}

# Save for reference
with open(pathlib.Path(__file__).resolve().parent / "direct_extraction_request.json", "w", encoding="utf-8") as f:
    json.dump(production_body, f, indent=2)

print("=" * 80)
print("DIRECT EXTRACTION OLLAMA TEST")
print("=" * 80)
print(f"Model: {production_body['model']}")
print(f"System prompt length: {len(production_body['messages'][0]['content'])} chars")
print(f"User message length: {len(production_body['messages'][1]['content'])} chars")
print(f"Has format: {'format' in production_body}")
print(f"Format schema size: {len(json.dumps(full_schema))} bytes")
print(f"Total body size: {len(json.dumps(production_body))} bytes")
print(f"Timeout: {TIMEOUT}s")
print()

# Test A: Direct requests to Ollama (3 runs)
print("=" * 80)
print("TEST A: Direct Ollama requests (3 runs)")
print("=" * 80)

direct_results = []
for i in range(3):
    print(f"\nDirect Ollama Run {i+1}:")
    t0 = time.perf_counter()
    
    try:
        # Use stream=False like the application provider (stream=True is for streaming responses)
        r = requests.post(OLLAMA_URL, json=production_body, timeout=TIMEOUT)
        total_time = time.perf_counter() - t0
        
        if r.status_code == 200:
            data = r.json()
            content = data.get("message", {}).get("content", "")
            
            print(f"  [SUCCESS]")
            print(f"    HTTP status: {r.status_code}")
            print(f"    Total time: {total_time:.3f}s")
            print(f"    Response size: {len(r.text)} bytes")
            print(f"    Content length: {len(content)} chars")
            print(f"    Done reason: {data.get('done_reason')}")
            
            direct_results.append({
                "run": i+1,
                "status": "SUCCESS",
                "status_code": r.status_code,
                "total_time_s": round(total_time, 3),
                "response_size": len(r.text),
                "content_length": len(content),
                "done_reason": data.get("done_reason"),
                "exception": None,
            })
        else:
            total_time = time.perf_counter() - t0
            print(f"  HTTP {r.status_code}")
            print(f"    Time: {total_time:.3f}s")
            
            direct_results.append({
                "run": i+1,
                "status": f"HTTP {r.status_code}",
                "status_code": r.status_code,
                "time_to_headers_s": round(headers_time, 3),
                "total_time_s": round(total_time, 3),
                "response_size": len(r.text),
                "content_length": 0,
                "done_reason": None,
                "exception": f"HTTP {r.status_code}",
            })
            
    except requests.Timeout:
        total_time = time.perf_counter() - t0
        print(f"  [TIMEOUT]")
        print(f"    Time: {total_time:.3f}s")
        
        direct_results.append({
            "run": i+1,
            "status": "TIMEOUT",
            "status_code": None,
            "total_time_s": round(total_time, 3),
            "response_size": 0,
            "content_length": 0,
            "done_reason": None,
            "exception": "requests.Timeout",
        })
    except Exception as exc:
        total_time = time.perf_counter() - t0
        print(f"  [ERROR] {type(exc).__name__}")
        print(f"    Time: {total_time:.3f}s")
        print(f"    Message: {str(exc)[:100]}")
        
        direct_results.append({
            "run": i+1,
            "status": type(exc).__name__,
            "status_code": None,
            "total_time_s": round(total_time, 3),
            "response_size": 0,
            "content_length": 0,
            "done_reason": None,
            "exception": f"{type(exc).__name__}: {str(exc)[:100]}",
        })

# Test B: Control request (simple OK) - qwen3:4b
print()
print("=" * 80)
print("TEST B: Control request (simple OK)")
print("=" * 80)

control_body = {
    "model": MODEL,
    "messages": [{"role": "user", "content": "Return the word OK"}],
    "options": {"temperature": 0.0, "num_predict": 20, "num_ctx": 12288},
    "stream": False,
    "keep_alive": "30m",
}

control_results = []
for i in range(3):
    print(f"\nControl Run {i+1}:")
    t0 = time.perf_counter()
    
    try:
        r = requests.post(OLLAMA_URL, json=control_body, timeout=TIMEOUT, stream=True)
        total_time = time.perf_counter() - t0
        
        if r.status_code == 200:
            text = r.text
            data = json.loads(text)
            content = data.get("message", {}).get("content", "")
            
            print(f"  [SUCCESS]: {total_time:.3f}s, content='{content.strip()[:50]}'")
            
            control_results.append({
                "run": i+1,
                "status": "SUCCESS",
                "total_time_s": round(total_time, 3),
                "content": content.strip(),
            })
        else:
            print(f"  HTTP {r.status_code}: {total_time:.3f}s")
            
            control_results.append({
                "run": i+1,
                "status": f"HTTP {r.status_code}",
                "total_time_s": round(total_time, 3),
            })
            
    except Exception as exc:
        total_time = time.perf_counter() - t0
        print(f"  [ERROR] {type(exc).__name__}: {total_time:.3f}s")
        
        control_results.append({
            "run": i+1,
            "status": type(exc).__name__,
            "total_time_s": round(total_time, 3),
        })

# Test C: Application Ollama provider with 60s timeout
print()
print("=" * 80)
print("TEST C: Application Ollama provider with 60s timeout")
print("=" * 80)

from corpdisc.agents.providers.ollama import OllamaProvider
from corpdisc.agents.ports import ProviderRequest, Message
from corpdisc.transport import JsonTransport
from corpdisc.services.llm_http import RequestsJsonTransport

# Create provider with 60s timeout
transport = RequestsJsonTransport()
provider = OllamaProvider(transport=transport, base_url="", timeout=60, num_ctx=12288, keep_alive="30m")

# Build request (similar to what extraction agent uses)
req = ProviderRequest(
    model=MODEL,
    system=production_body["messages"][0]["content"],
    messages=(Message("user", production_body["messages"][1]["content"]),),
    response_schema=full_schema,
    schema_name="ExtractionOutput",
    temperature=production_body["options"]["temperature"],
    max_output_tokens=production_body["options"]["num_predict"],
    tools=(),
    reasoning="none",
)

print(f"Provider: {provider}")
print(f"Request schema: {req.schema_name}")
print(f"Request size: {len(json.dumps(production_body))} bytes (same as direct)")
print()

try:
    t0 = time.perf_counter()
    resp = provider.complete(req)
    total_time = time.perf_counter() - t0
    content = resp.content or ""
    
    print(f"  ✅ SUCCESS")
    print(f"    Total time: {total_time:.3f}s")
    print(f"    Content length: {len(content)} chars")
    print(f"    Finish reason: {resp.finish_reason}")
    print(f"    Model: {resp.model}")
    
    if content:
        print(f"    Preview: {content[:200]}")
        
except Exception as exc:
    total_time = time.perf_counter() - t0
    print(f"  [ERROR] {type(exc).__name__}")
    print(f"    Total time: {total_time:.3f}s")
    print(f"    Message: {str(exc)[:300]}")
    
    # Check if it's a TransportFailure
    from corpdisc.transport import TransportFailure
    if isinstance(exc, TransportFailure):
        print(f"    TransportFailure.code: {exc.code}")
        print(f"    TransportFailure.message: {exc.args[0]}")

# Summary
print()
print("=" * 80)
print("SUMMARY")
print("=" * 80)

print("\nDirect Ollama Results:")
for r in direct_results:
    if r["status"] == "SUCCESS":
        status_str = f"SUCCESS ({r['total_time_s']:.3f}s)"
    else:
        status_str = r["status"]
    print(f"  Run {r['run']}: {status_str}")

print("\nControl Results:")
for r in control_results:
    if r["status"] == "SUCCESS":
        status_str = f"SUCCESS ({r['total_time_s']:.3f}s)"
    else:
        status_str = r["status"]
    print(f"  Run {r['run']}: {status_str}")

print("\nApplication Provider Result:")
print(f"  Provider completed: {'[SUCCESS]' if 'provider' in dir() and 'resp' in dir() else '[FAILED]'}")

# Determine root cause
print()
print("=" * 80)
print("ROOT CAUSE DETERMINATION")
print("=" * 80)

success_count = sum(1 for r in direct_results if r["status"] == "SUCCESS")
control_success_count = sum(1 for r in control_results if r["status"] == "SUCCESS")

print(f"Direct Ollama success rate: {success_count}/3")
print(f"Control success rate: {control_success_count}/3")

if success_count == 3:
    print("\nCASE A: Direct Ollama works consistently -> PROBLEM IS NOT OLLAMA")
    print("The issue is in the application provider/transport implementation.")
elif success_count == 0:
    print("\nCASE A: Direct Ollama fails consistently -> PROBLEM IS OLLAMA/MODEL")
    print("Ollama itself has an issue with this request pattern.")
else:
    print(f"\nINCONSISTENT: Direct Ollama success rate is {success_count}/3")
    print("This indicates a stochastic issue with Ollama or system resources.")

# Save results
results = {
    "direct_ollama": direct_results,
    "control": control_results,
    "production_body": production_body,
}

results_path = pathlib.Path(__file__).resolve().parent / "direct_extraction_test_results.json"
with open(results_path, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2)
print(f"\nResults saved to: {results_path}")
