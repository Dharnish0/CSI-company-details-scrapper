#!/usr/bin/env python
"""Controlled experiments for Ollama structured extraction diagnosis."""

from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import requests


def test_ollama_chat(
    *,
    base_url: str,
    model: str,
    system_prompt: str,
    user_message: str,
    response_schema: dict | None,
    num_ctx: int = 12288,
    num_predict: int = 3000,
    temperature: float = 0.0,
    think: str | None = "none",
    timeout: float = 300.0,
) -> dict:
    """Test Ollama /api/chat with various configurations."""
    url = f"{base_url.rstrip('/')}/api/chat"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    body: dict[str, Any] = {
        "model": model,
        "stream": False,
        "messages": messages,
        "options": {
            "temperature": temperature,
            "num_predict": num_predict,
            "num_ctx": num_ctx,
        },
    }

    if response_schema is not None:
        body["format"] = response_schema

    if think is not None:
        body["think"] = think.strip().lower() not in ("none", "off", "false", "disabled")

    raw_request = json.dumps(body)

    t0 = time.perf_counter()
    try:
        resp = requests.post(url, json=body, timeout=timeout)
        wall_s = round(time.perf_counter() - t0, 1)
        try:
            data = resp.json()
        except ValueError:
            data = {"raw_response": resp.text[:2000], "status_code": resp.status_code}

        return {
            "model": model,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
            "has_format": response_schema is not None,
            "think": think,
            "wall_s": wall_s,
            "status_code": resp.status_code,
            "request_bytes": len(raw_request.encode("utf-8")),
            "response": data,
        }
    except requests.exceptions.Timeout:
        wall_s = round(time.perf_counter() - t0, 1)
        return {
            "model": model,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
            "has_format": response_schema is not None,
            "think": think,
            "wall_s": wall_s,
            "status_code": "TIMEOUT",
            "request_bytes": len(raw_request.encode("utf-8")),
            "response": {"error": "timeout"},
        }
    except requests.exceptions.RequestException as exc:
        wall_s = round(time.perf_counter() - t0, 1)
        return {
            "model": model,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
            "has_format": response_schema is not None,
            "think": think,
            "wall_s": wall_s,
            "status_code": type(exc).__name__,
            "request_bytes": len(raw_request.encode("utf-8")),
            "response": {"error": str(exc)[:500]},
        }


def build_extraction_prompt() -> str:
    """Build a realistic extraction prompt."""
    return (
        "You are analyzing a webpage about corporate entities. "
        "The parent company is Constellation Software Inc. "
        "Extract any companies, subsidiaries, or corporate entities mentioned on this page. "
        "Return a JSON object with an 'entities' array (each with 'name', 'kind', 'proposed_type', 'is_corporate_entity', 'claim', 'reason') "
        "and a 'rejected' array for non-entity items like products, services, or industries. "
        "Page text: The page lists several subsidiaries including CSI Healthcare, CSI Logistics, and CSI Technology. "
        "It also mentions the product 'CSI Analytics' and the service 'Cloud Solutions'."
    )


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:11434")
    ap.add_argument("--model", default="mistral:latest")
    ap.add_argument("--num-ctx", type=int, default=12288)
    ap.add_argument("--num-predict", type=int, default=3000)
    args = ap.parse_args()

    # Load schema
    schema_path = pathlib.Path(__file__).resolve().parent.parent / "diagnostics" / "extraction_output_schema.json"
    if not schema_path.exists():
        print("ERROR: Run extract_schema.py first to generate the schema")
        sys.exit(1)

    with open(schema_path) as f:
        schema = json.load(f)

    base_url = args.base
    model = args.model
    prompt = build_extraction_prompt()

    print("=" * 80)
    print("OLLAMA CONTROLLED EXPERIMENTS")
    print("=" * 80)
    print(f"\nBase URL: {base_url}")
    print(f"Model: {model}")
    print(f"Schema loaded from: {schema_path}")
    print(f"Schema byte size: {len(json.dumps(schema, separators=(',', ':')).encode('utf-8')):,}")

    # Test matrix
    tests = [
        # Basic plain text
        ("A1", "plain text, no format", {"num_predict": 100}, None, "none"),
        # Simple JSON without format
        ("A2", "simple JSON, no format", {"num_predict": 100}, None, "none"),
        # Simple JSON with format
        ("A3", "simple JSON, WITH format", {"num_predict": 100}, {"type": "object", "properties": {"name": {"type": "string"}}}, "none"),
        # ExtractionOutput with various num_predict
        ("B1", "ExtractionOutput, num_predict=100", {"num_predict": 100}, schema, "none"),
        ("B2", "ExtractionOutput, num_predict=500", {"num_predict": 500}, schema, "none"),
        ("B3", "ExtractionOutput, num_predict=1000", {"num_predict": 1000}, schema, "none"),
        ("B4", "ExtractionOutput, num_predict=3000", {"num_predict": 3000}, schema, "none"),
        # ExtractionOutput with various num_ctx
        ("C1", "ExtractionOutput, num_ctx=4096", {"num_ctx": 4096}, schema, "none"),
        ("C2", "ExtractionOutput, num_ctx=8192", {"num_ctx": 8192}, schema, "none"),
        ("C3", "ExtractionOutput, num_ctx=12288", {"num_ctx": args.num_ctx}, schema, "none"),
        # With think enabled
        ("D1", "ExtractionOutput WITH think=default", {"num_predict": 1000}, schema, "default"),
        # Without format
        ("E1", "ExtractionOutput WITHOUT format", {"num_predict": 3000}, None, "none"),
    ]

    results = []
    for label, description, overrides, schema_override, think in tests:
        num_ctx = overrides.get("num_ctx", args.num_ctx)
        num_predict = overrides.get("num_predict", args.num_predict)

        print(f"\n{'='*80}")
        print(f"TEST {label}: {description}")
        print(f"{'='*80}")
        print(f"  num_ctx={num_ctx}, num_predict={num_predict}, has_format={schema_override is not None}, think={think}")

        resp = test_ollama_chat(
            base_url=base_url,
            model=model,
            system_prompt=prompt,
            user_message="Return a JSON object matching the required schema.",
            response_schema=schema_override,
            num_ctx=num_ctx,
            num_predict=num_predict,
            timeout=300.0,
            think=think,
        )

        results.append({"label": label, "description": description, **resp})

        print(f"  Wall time: {resp['wall_s']}s")
        print(f"  Status: {resp['status_code']}")

        if isinstance(resp["response"], dict):
            if "message" in resp["response"]:
                msg = resp["response"]["message"]
                content = msg.get("content", "") if isinstance(msg, dict) else ""
                print(f"  Content length: {len(content)} chars")
                if content:
                    print(f"  Content preview: {content[:200]}...")
            elif "error" in resp["response"]:
                print(f"  Error: {resp['response']['error'][:300]}")

        # Save individual result
        result_path = pathlib.Path(__file__).resolve().parent.parent / "diagnostics" / f"experiment_{label}.json"
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(resp, f, indent=2)
        print(f"  Result saved to: {result_path}")

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    for r in results:
        status = r["status_code"]
        if status == 200:
            status_str = "OK"
        elif status == "TIMEOUT":
            status_str = "TIMEOUT"
        elif isinstance(status, int):
            status_str = f"HTTP {status}"
        else:
            status_str = str(status)

        print(f"{r['label']:4} | {r['wall_s']:6.1f}s | {status_str:12} | {r['description']}")
