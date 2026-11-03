#!/usr/bin/env python
"""Test whether the extraction schema causes the Ollama no-response."""

from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import requests
import json

from corpdisc.contracts.agent_io import ExtractionOutput

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "mistral:latest"
TIMEOUT = 30

def test(description, system_prompt, user_content, response_schema, timeout):
    """Test with/without schema."""
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "options": {
            "temperature": 0.0,
            "num_predict": 100,
            "num_ctx": 12288,
        },
        "stream": False,
        "keep_alive": "30m",
    }
    
    if response_schema is not None:
        body["format"] = response_schema
    
    request_bytes = len(json.dumps(body).encode("utf-8"))
    
    print(f"  {description}:")
    print(f"    Has format: {response_schema is not None}")
    print(f"    Request bytes: {request_bytes}")
    
    t0 = time.perf_counter()
    try:
        r = requests.post(OLLAMA_URL, json=body, timeout=timeout, stream=True)
        wall = time.perf_counter() - t0
        
        if r.status_code == 200:
            chunks = []
            for chunk in r.iter_content(chunk_size=65536):
                chunks.append(chunk)
            text = b"".join(chunks).decode("utf-8")
            data = json.loads(text)
            content = data.get("message", {}).get("content", "")
            
            print(f"    ✅ SUCCESS: {wall:.1f}s, content={len(content)} chars")
            return True
        else:
            print(f"    HTTP {r.status_code} after {wall:.1f}s")
            return False
            
    except requests.Timeout:
        wall = time.perf_counter() - t0
        print(f"    ❌ TIMEOUT after {wall:.1f}s")
        return False
    except Exception as exc:
        wall = time.perf_counter() - t0
        print(f"    ❌ {type(exc).__name__} after {wall:.1f}s")
        return False


def main():
    print("=" * 80)
    print("SCHEMA TEST - Does response_schema cause Ollama no-response?")
    print("=" * 80)
    
    # Simple system prompt
    simple_system = "Extract entities. Return JSON."
    
    # Simple user content
    user_content = '{"page_text":"Foxtrot Analytics is an operating group of Constellation Software Inc..","candidates":[{"id":1,"name":"Foxtrot Analytics"}]}'
    
    # Get the extraction schema
    schema = ExtractionOutput.model_json_schema()
    print(f"\nExtraction schema: {len(json.dumps(schema))} bytes")
    print(f"Schema $defs: {len(schema.get('$defs', {}))} keys")
    print(f"Schema $refs: {sum(1 for v in str(schema).split() if '$ref' in v)}")
    
    print("\n" + "=" * 80)
    print("TEST A: NO schema")
    print("=" * 80)
    test("NO schema", simple_system, user_content, None, TIMEOUT)
    
    print("\n" + "=" * 80)
    print("TEST B: Simple schema")
    print("=" * 80)
    simple_schema = {
        "type": "object",
        "properties": {
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "kind": {"type": "string", "enum": ["COMPANY", "GROUP", "BRAND", "PRODUCT"]},
                    },
                    "required": ["name", "kind"],
                },
            },
        },
        "required": ["entities"],
    }
    test("Simple schema", simple_system, user_content, simple_schema, TIMEOUT)
    
    print("\n" + "=" * 80)
    print("TEST C: Full ExtractionOutput schema (with format)")
    print("=" * 80)
    test("Full schema", simple_system, user_content, schema, TIMEOUT)
    
    print("\n" + "=" * 80)
    print("TEST D: Full ExtractionOutput schema without format (just prompt)")
    print("=" * 80)
    # Same as C but without format field
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": simple_system + "\n\nSchema:\n" + json.dumps(schema)},
            {"role": "user", "content": user_content},
        ],
        "options": {
            "temperature": 0.0,
            "num_predict": 100,
            "num_ctx": 12288,
        },
        "stream": False,
        "keep_alive": "30m",
    }
    
    print(f"    Request bytes: {len(json.dumps(body).encode('utf-8'))}")
    
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
            
            print(f"    ✅ SUCCESS: {wall:.1f}s, content={len(content)} chars")
        else:
            print(f"    HTTP {r.status_code} after {wall:.1f}s")
            
    except requests.Timeout:
        wall = time.perf_counter() - t0
        print(f"    ❌ TIMEOUT after {wall:.1f}s")
    except Exception as exc:
        wall = time.perf_counter() - t0
        print(f"    ❌ {type(exc).__name__} after {wall:.1f}s")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
