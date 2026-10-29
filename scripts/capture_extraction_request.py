#!/usr/bin/env python
"""Capture the exact ProviderRequest from the real extraction agent and run controlled experiments."""

from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# Monkey-patch OllamaProvider to capture the request
original_complete = None
captured_requests = []
extraction_requests = []


def capture_complete(self, request):
    """Capture the exact request before sending."""
    body = self.build_body(request)
    raw = json.dumps(body)
    req_info = {
        "model": request.model,
        "system_prompt_length": len(request.system),
        "system_prompt_preview": request.system[:500] if len(request.system) > 500 else request.system,
        "user_message_count": len(request.messages),
        "user_message_total_length": sum(len(m.content) for m in request.messages),
        "user_messages_preview": [(m.role, m.content[:300]) for m in request.messages[:3]],
        "response_schema": request.response_schema,
        "response_schema_size": len(json.dumps(request.response_schema)),
        "response_schema_name": request.schema_name,
        "temperature": request.temperature,
        "max_output_tokens": request.max_output_tokens,
        "num_ctx": self._num_ctx,
        "think": request.reasoning,
        "tools": bool(request.tools),
        "keep_alive": self._keep_alive,
        "serialized_request_bytes": len(raw.encode("utf-8")),
    }
    
    # Count schema characteristics
    schema = request.response_schema
    req_info["schema_$defs"] = len(schema.get("$defs", {}))
    req_info["schema_$refs"] = count_refs(schema)
    req_info["schema_anyOf"] = count_anyof(schema)
    req_info["schema_arrays"] = count_arrays(schema)
    req_info["schema_enums"] = count_enums(schema)
    
    captured_requests.append(req_info)
    
    # Store for extraction agent specifically
    if request.schema_name == "ExtractionOutput":
        extraction_requests.append(req_info)
        print(f"\n{'='*80}")
        print("EXTRACTION REQUEST CAPTURED")
        print(f"{'='*80}")
        print(f"Model: {req_info['model']}")
        print(f"System prompt length: {req_info['system_prompt_length']} chars")
        print(f"User messages: {req_info['user_message_count']} (total {req_info['user_message_total_length']} chars)")
        print(f"Response schema: {req_info['response_schema_name']} ({req_info['response_schema_size']} bytes)")
        print(f"Schema $defs: {req_info['schema_$defs']}, $refs: {req_info['schema_$refs']}, anyOf: {req_info['schema_anyOf']}")
        print(f"Serialized request: {req_info['serialized_request_bytes']} bytes")
        print(f"num_ctx: {req_info['num_ctx']}, num_predict: {req_info['max_output_tokens']}, temperature: {req_info['temperature']}")
        print(f"think/reasoning: {req_info['think']}")
        print(f"keep_alive: {req_info['keep_alive']}")
        
        # Count specific schema elements
        print(f"\nSchema analysis:")
        print(f"  Top-level keys: {list(schema.keys())}")
        if "$defs" in schema:
            print(f"  $defs keys: {list(schema['$defs'].keys())}")
        print(f"  properties: {list(schema.get('properties', {}).keys())}")
        
        # Save to file
        out_dir = pathlib.Path(__file__).resolve().parent.parent / "diagnostics"
        out_dir.mkdir(exist_ok=True)
        
        with open(out_dir / "extraction_request.json", "w", encoding="utf-8") as f:
            json.dump(req_info, f, indent=2)
        
        with open(out_dir / "extraction_raw_request.json", "w", encoding="utf-8") as f:
            json.dump(json.loads(raw), f, indent=2)
        
        print(f"\nSaved to: {out_dir}/extraction_request.json")
        print(f"Saved to: {out_dir}/extraction_raw_request.json")
    
    # Continue with original request
    return original_complete(self, request)


def count_refs(obj, path=""):
    """Count $ref occurrences in schema."""
    if isinstance(obj, dict):
        count = sum(count_refs(v, f"{path}.{k}") for k, v in obj.items())
        if "$ref" in obj:
            count += 1
        return count
    if isinstance(obj, list):
        return sum(count_refs(item, f"{path}[]") for item in obj)
    return 0


def count_anyof(obj, path=""):
    """Count anyOf occurrences in schema."""
    if isinstance(obj, dict):
        count = sum(count_anyof(v, f"{path}.{k}") for k, v in obj.items())
        if "anyOf" in obj:
            count += 1
        return count
    if isinstance(obj, list):
        return sum(count_anyof(item, f"{path}[]") for item in obj)
    return 0


def count_arrays(obj):
    """Count array type occurrences in schema."""
    if isinstance(obj, dict):
        count = sum(count_arrays(v) for v in obj.values())
        if obj.get("type") == "array":
            count += 1
        return count
    if isinstance(obj, list):
        return sum(count_arrays(item) for item in obj)
    return 0


def count_enums(obj):
    """Count enum occurrences in schema."""
    if isinstance(obj, dict):
        count = sum(count_enums(v) for v in obj.values())
        if "enum" in obj:
            count += 1
        return count
    if isinstance(obj, list):
        return sum(count_enums(item) for item in obj)
    return 0


def main():
    from corpdisc.agents.providers import build_provider_from_settings
    from corpdisc.app import build_runtime
    from corpdisc.config import Settings
    from tests.fixtures.listing_site import site
    
    # Monkey-patch before building runtime
    from corpdisc.agents.providers import ollama as ollama_module
    global original_complete
    original_complete = ollama_module.OllamaProvider.complete
    ollama_module.OllamaProvider.complete = capture_complete
    
    # Run extraction
    print("Running extraction to capture the request...")
    
    from tests.fixtures.listing_site import site
    
    st = Settings(_env_file=None, start_url="https://root.test/", root_company="Constellation Software Inc.",
                  root_code="CSI", request_delay_seconds=0.0, max_retries=0,
                  db_path=pathlib.Path("preflight_out/extraction_capture.db"),
                  cache_dir=pathlib.Path("preflight_out/.cache_capture"),
                  log_dir=pathlib.Path("preflight_out/logs"), use_playwright="false",
                  llm_provider="ollama", llm_model="mistral:latest", llm_extraction_reasoning="none")
    
    rt = build_runtime(st, http=site(8), transport=None)
    rt.supervisor.seed_root()
    rt.supervisor.run(max_tasks=1)
    rt.close()
    
    print(f"\n{'='*80}")
    print(f"Captured {len(extraction_requests)} extraction request(s)")
    
    if extraction_requests:
        req = extraction_requests[0]
        print(f"\n{'='*80}")
        print("EXTRACTION REQUEST SUMMARY")
        print(f"{'='*80}")
        print(json.dumps(req, indent=2))
        
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
