#!/usr/bin/env python
"""Controlled experiments with the exact real extraction request."""

from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import requests


def run_experiment(
    label: str,
    model: str,
    system_content: str,
    user_content: str,
    options: dict,
    messages_override: list | None = None,
    format_schema: dict | None = None,
    timeout: float = 300.0,
) -> dict:
    """Run one experiment against Ollama."""
    base_url = "http://localhost:11434"
    
    messages = messages_override or [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]
    
    body = {
        "model": model,
        "stream": False,
        "options": options,
        "messages": messages,
        "keep_alive": "30m",
    }
    
    if format_schema is not None:
        body["format"] = format_schema
    
    raw = json.dumps(body)
    
    print(f"\n{'='*80}")
    print(f"EXPERIMENT {label}")
    print(f"{'='*80}")
    print(f"Model: {model}")
    print(f"System prompt length: {len(system_content)} chars")
    print(f"User message length: {len(user_content)} chars")
    print(f"Options: {options}")
    print(f"Has format: {format_schema is not None}")
    if format_schema:
        print(f"Format schema size: {len(json.dumps(format_schema))} bytes")
    print(f"Total request bytes: {len(raw.encode('utf-8'))}")
    
    t0 = time.perf_counter()
    try:
        resp = requests.post(f"{base_url}/api/chat", json=body, timeout=timeout)
        wall_s = round(time.perf_counter() - t0, 1)
        
        try:
            data = resp.json()
        except ValueError:
            data = {"raw_response": resp.text[:2000], "status_code": resp.status_code}
        
        return {
            "label": label,
            "wall_s": wall_s,
            "status_code": resp.status_code,
            "response": data,
            "request_bytes": len(raw.encode("utf-8")),
        }
    except requests.exceptions.Timeout:
        wall_s = round(time.perf_counter() - t0, 1)
        return {
            "label": label,
            "wall_s": wall_s,
            "status_code": "TIMEOUT",
            "response": {"error": "timeout"},
            "request_bytes": len(raw.encode("utf-8")),
        }
    except requests.exceptions.RequestException as exc:
        wall_s = round(time.perf_counter() - t0, 1)
        return {
            "label": label,
            "wall_s": wall_s,
            "status_code": type(exc).__name__,
            "response": {"error": str(exc)[:500]},
            "request_bytes": len(raw.encode("utf-8")),
        }


def main():
    # Load the exact real request
    raw_path = pathlib.Path(__file__).resolve().parent.parent / "diagnostics" / "extraction_raw_request.json"
    with open(raw_path) as f:
        real_request = json.load(f)
    
    model = real_request["model"]
    system_content = real_request["messages"][0]["content"]
    user_content = real_request["messages"][1]["content"]
    options = real_request["options"]
    
    # Load the schema
    schema_path = pathlib.Path(__file__).resolve().parent.parent / "diagnostics" / "extraction_request.json"
    with open(schema_path) as f:
        req_info = json.load(f)
    schema = req_info["response_schema"]
    
    print("=" * 80)
    print("CONTROLLED EXPERIMENTS WITH REAL EXTRACTION REQUEST")
    print("=" * 80)
    print(f"Base URL: http://localhost:11434")
    print(f"Model: {model}")
    print(f"System prompt: {len(system_content)} chars")
    print(f"User message: {len(user_content)} chars")
    print(f"Options: {options}")
    print(f"Schema: {req_info['response_schema_name']} ({req_info['response_schema_size']} bytes)")
    print(f"Schema $defs: {req_info['schema_$defs']}, $refs: {req_info['schema_$refs']}, anyOf: {req_info['schema_anyOf']}")
    
    # Run experiments
    results = []
    
    # A. Exact real extraction request, WITHOUT format
    results.append(run_experiment("A", model, system_content, user_content, options, format_schema=None))
    
    # B. Same request with a tiny simple JSON format
    simple_schema = {"type": "object", "properties": {"test": {"type": "string"}}, "required": ["test"]}
    results.append(run_experiment("B", model, system_content, user_content, options, format_schema=simple_schema))
    
    # C. Same request with the full ExtractionOutput JSON schema as format
    results.append(run_experiment("C", model, system_content, user_content, options, format_schema=schema))
    
    # D. Same messages but remove the schema text from the system prompt
    system_no_schema = "You are an extraction agent. Reply with JSON."
    results.append(run_experiment("D", model, system_no_schema, user_content, options, format_schema=None))
    
    # E. Same request with a drastically smaller num_ctx
    options_4096 = {**options, "num_ctx": 4096}
    results.append(run_experiment("E", model, system_content, user_content, options_4096, format_schema=None))
    
    # F. Same request with a very small max output
    options_500 = {**options, "num_predict": 500}
    results.append(run_experiment("F", model, system_content, user_content, options_500, format_schema=None))
    
    # G. Same request with a minimal system prompt and the exact extraction user content
    system_minimal = "Extract corporate entities. Return JSON matching the schema."
    results.append(run_experiment("G", model, system_minimal, user_content, options, format_schema=None))
    
    # Summary
    print("\n" + "=" * 80)
    print("EXPERIMENT SUMMARY")
    print("=" * 80)
    print(f"{'Label':<6} | {'Wall (s)':<10} | {'Status':<15} | {'Response size':<15} | Description")
    print("-" * 80)
    
    for r in results:
        status = r["status_code"]
        if status == 200:
            resp_data = r["response"]
            if isinstance(resp_data, dict) and "message" in resp_data:
                content = resp_data.get("message", {}).get("content", "")
                resp_size = len(content) if content else 0
                status_str = f"200 ({len(content)} chars)"
            else:
                resp_size = len(str(resp_data))
                status_str = f"200"
        else:
            status_str = str(status)
            resp_size = 0
        
        desc = {
            "A": "Real extraction, NO format",
            "B": "Real extraction, simple format",
            "C": "Real extraction, FULL format schema",
            "D": "Minimal system prompt, NO format",
            "E": "num_ctx=4096, NO format",
            "F": "num_predict=500, NO format",
            "G": "Minimal system + real user, NO format",
        }.get(r["label"], "")
        
        print(f"{r['label']:<6} | {r['wall_s']:>8.1f}s | {status_str:<15} | {resp_size:>13}B | {desc}")
    
    # Save results
    results_path = pathlib.Path(__file__).resolve().parent.parent / "diagnostics" / "experiment_results.json"
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"\nFull results saved to: {results_path}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
