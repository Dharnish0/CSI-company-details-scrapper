#!/usr/bin/env python
"""Capture the exact exception from a failed extraction."""

from __future__ import annotations

import json
import pathlib
import sys
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from corpdisc.agents.providers import build_provider_from_settings
from corpdisc.app import build_runtime
from corpdisc.config import Settings
from corpdisc.agents.providers.ollama import OllamaProvider


class CapturingProvider:
    """Wrapper that captures the exact request before sending."""
    
    def __init__(self, provider):
        self._provider = provider
    
    def complete(self, request):
        body = self._provider.build_body(request)
        raw = json.dumps(body)
        
        # Save the exact request
        out_dir = pathlib.Path(__file__).resolve().parent.parent / "diagnostics"
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
            "num_ctx": self._provider._num_ctx,
            "reasoning": request.reasoning,
            "tools": bool(request.tools),
            "keep_alive": self._provider._keep_alive,
            "serialized_request_bytes": len(raw.encode("utf-8")),
            "raw_request": json.loads(raw),
        }
        
        # Save for extraction
        if request.schema_name == "ExtractionOutput":
            with open(out_dir / "exact_extraction_request.json", "w", encoding="utf-8") as f:
                json.dump(request_info, f, indent=2)
            print(f"Saved exact request to: {out_dir / 'exact_extraction_request.json'}")
        
        try:
            return self._provider.complete(request)
        except Exception as exc:
            # Capture full traceback
            print(f"\n{'='*80}")
            print("EXCEPTION CAUGHT")
            print(f"{'='*80}")
            print(f"Exception type: {type(exc).__module__}.{type(exc).__name__}")
            print(f"Exception message: {str(exc)[:500]}")
            print("\nFull traceback:")
            traceback.print_exc()
            
            # Save exception details
            with open(out_dir / "exception_details.txt", "w", encoding="utf-8") as f:
                f.write(f"Exception type: {type(exc).__module__}.{type(exc).__name__}\n")
                f.write(f"Exception message: {str(exc)}\n\n")
                f.write("Full traceback:\n")
                traceback.print_exc(file=f)
            
            raise


def main():
    print("Running extraction with exception capture...")
    
    st = Settings(_env_file=None, start_url="https://root.test/", root_company="Constellation Software Inc.",
                  root_code="CSI", request_delay_seconds=0.0, max_retries=0,
                  db_path=pathlib.Path("preflight_out/exception_capture.db"),
                  cache_dir=pathlib.Path("preflight_out/.cache_exc"),
                  log_dir=pathlib.Path("preflight_out/logs"), use_playwright="false",
                  llm_provider="ollama", llm_model="mistral:latest", llm_extraction_reasoning="none")
    
    # Build provider
    from corpdisc.transport import JsonTransport
    from corpdisc.services.llm_http import RequestsJsonTransport
    
    transport = RequestsJsonTransport()
    provider = build_provider_from_settings(st, transport=transport)
    provider._keep_alive = "30m"  # Ensure keep_alive is set
    
    # Wrap with capturing provider
    capturing_provider = CapturingProvider(provider)
    
    # Build runtime with capturing provider
    from corpdisc.agents.ports import LLMProvider
    from corpdisc.agents.gateway import LLMGateway, GatewayPolicy, CircuitBreaker
    from corpdisc.agents.runner import AgentRunner, RunnerLimits
    from corpdisc.agents.broker import ToolBroker
    from corpdisc.clock import RealClock, RealSleeper
    from corpdisc.services.budget import BudgetGuard
    from corpdisc.db.repos.audit import SqliteAuditSink
    from corpdisc.db.repos.cache import SqliteResponseCache
    from tests.fixtures.listing_site import site
    
    class MockClock:
        def now(self): from datetime import datetime; return datetime.now()
        def monotonic(self): import time; return time.monotonic()
    
    class MockSleeper:
        def sleep(self, seconds): import time; time.sleep(seconds)
    
    # Build the runtime manually
    rt = build_runtime(st, http=site(8), transport=None)
    
    # Replace provider with capturing version
    rt.deps.runner._gateway._provider = capturing_provider
    
    # Run extraction
    print("\nStarting extraction...")
    rt.supervisor.seed_root()
    try:
        result = rt.supervisor.run(max_tasks=1)
        print(f"\nResult: {result}")
    except Exception as exc:
        print(f"\nRuntime exception: {type(exc).__name__}: {exc}")
    finally:
        rt.close()
    
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
