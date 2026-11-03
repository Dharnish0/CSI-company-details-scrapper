#!/usr/bin/env python
"""Capture the exact ProviderRequest and HTTP payload for the extraction agent."""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

# Monkey-patch to capture the request
original_complete = None
captured_requests = []


def capture_ollama_provider_complete(self, request):
    """Capture the exact request before sending."""
    body = self.build_body(request)
    raw = json.dumps(body)
    
    # Save the exact request
    out_dir = pathlib.Path(__file__).resolve().parent
    out_dir.mkdir(exist_ok=True)
    
    request_info = {
        "model": request.model,
        "system_prompt_length": len(request.system),
        "user_message_count": len(request.messages),
        "user_message_total_length": sum(len(m.content) for m in request.messages),
        "response_schema_name": request.schema_name,
        "response_schema_size": len(json.dumps(request.response_schema)),
        "temperature": request.temperature,
        "max_output_tokens": request.max_output_tokens,
        "num_ctx": self._num_ctx,
        "reasoning": request.reasoning,
        "tools": bool(request.tools),
        "keep_alive": self._keep_alive,
        "serialized_request_bytes": len(raw.encode("utf-8")),
        "compact_schema": json.dumps(request.response_schema, separators=(",", ":")),
        "raw_body": json.loads(raw),
        "body_json_string": raw,
    }
    
    captured_requests.append(request_info)
    
    # For extraction output, save to file
    if request.schema_name == "ExtractionOutput":
        with open(out_dir / "extraction_provider_request.json", "w", encoding="utf-8") as f:
            json.dump(request_info, f, indent=2)
        
        with open(out_dir / "extraction_raw_payload.json", "w", encoding="utf-8") as f:
            f.write(raw)
        
        print(f"\n{'='*80}")
        print("EXTRACTION REQUEST CAPTURED")
        print(f"{'='*80}")
        print(f"Model: {request_info['model']}")
        print(f"System prompt length: {request_info['system_prompt_length']} chars")
        print(f"User messages: {request_info['user_message_count']} (total {request_info['user_message_total_length']} chars)")
        print(f"Response schema: {request_info['response_schema_name']} ({request_info['response_schema_size']} bytes)")
        print(f"Has tools: {request_info['tools']}")
        print(f"Has format: {'format' in request_info['raw_body']}")
        print(f"Has think: {'think' in request_info['raw_body']}")
        print(f"Serialized request: {request_info['serialized_request_bytes']} bytes")
        print(f"\nSaved to: {out_dir}/extraction_provider_request.json")
        print(f"Saved to: {out_dir}/extraction_raw_payload.json")
    
    # Continue with original request
    return original_complete(self, request)


def main():
    from corpdisc.agents.providers.ollama import OllamaProvider
    from corpdisc.app import build_runtime
    from corpdisc.config import Settings
    
    # Monkey-patch
    global original_complete
    original_complete = OllamaProvider.complete
    OllamaProvider.complete = capture_ollama_provider_complete
    
    print("Starting extraction to capture the exact request...")
    
    st = Settings(_env_file=None, start_url="https://root.test/", root_company="Constellation Software Inc.",
                  root_code="CSI", request_delay_seconds=0.0, max_retries=0,
                  db_path=pathlib.Path("diagnostics/extraction_capture.db"),
                  cache_dir=pathlib.Path("diagnostics/.cache_capture"),
                  log_dir=pathlib.Path("diagnostics/logs"), use_playwright="false",
                  llm_provider="ollama", llm_model="mistral:latest", llm_extraction_reasoning="none")
    
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tests"))
    from fixtures.listing_site import site
    rt = build_runtime(st, http=site(8), transport=None)
    rt.supervisor.seed_root()
    rt.supervisor.run(max_tasks=1)
    rt.close()
    
    print(f"\n{'='*80}")
    print(f"Captured {len(captured_requests)} request(s)")
    
    if captured_requests:
        req = captured_requests[-1]
        print(f"\n{'='*80}")
        print("LAST CAPTURED REQUEST SUMMARY")
        print(f"{'='*80}")
        print(json.dumps({
            "model": req["model"],
            "system_prompt_length": req["system_prompt_length"],
            "user_message_count": req["user_message_count"],
            "user_message_total_length": req["user_message_total_length"],
            "response_schema_name": req["response_schema_name"],
            "response_schema_size": req["response_schema_size"],
            "temperature": req["temperature"],
            "max_output_tokens": req["max_output_tokens"],
            "num_ctx": req["num_ctx"],
            "reasoning": req["reasoning"],
            "tools": req["tools"],
            "keep_alive": req["keep_alive"],
            "serialized_request_bytes": req["serialized_request_bytes"],
            "has_format": "format" in req["raw_body"],
            "has_think": "think" in req["raw_body"],
        }, indent=2))
        
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
