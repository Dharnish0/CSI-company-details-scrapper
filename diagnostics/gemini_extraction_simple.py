#!/usr/bin/env python
"""Quick Gemini extraction test."""
import pathlib
import sys
sys.path.insert(0, str(pathlib.Path.cwd().resolve() / 'src'))

from corpdisc.config import Settings
from corpdisc.agents.providers import build_provider_from_settings
from corpdisc.agents.ports import ProviderRequest, Message
from corpdisc.contracts.agent_io import ExtractionOutput
from corpdisc.services.llm_http import RequestsJsonTransport
import json
import time

# Load settings
settings = Settings(_env_file='.env')

# Load extraction request
with open(pathlib.Path('diagnostics/extraction_raw_request.json')) as f:
    base_body = json.load(f)

full_schema = ExtractionOutput.model_json_schema()

# Build request
req = ProviderRequest(
    model=settings.gemini_model,
    system=base_body['messages'][0]['content'],
    messages=(Message('user', base_body['messages'][1]['content']),),
    response_schema=full_schema,
    schema_name='ExtractionOutput',
    temperature=base_body['options']['temperature'],
    max_output_tokens=base_body['options']['num_predict'],
    tools=(),
    reasoning='none',
)

# Build provider
transport = RequestsJsonTransport()
provider = build_provider_from_settings(settings, transport=transport)

# Test
t0 = time.perf_counter()
try:
    resp = provider.complete(req)
    total_time = time.perf_counter() - t0
    content = resp.content or ''
    
    print('=== GEMINI EXTRACTION TEST RESULT ===')
    print(f'Model: {settings.gemini_model}')
    print(f'Total latency: {total_time:.3f}s')
    print(f'Content length: {len(content)} chars')
    print(f'Finish reason: {resp.finish_reason}')
    
    # Show token usage
    if resp.metrics:
        pt = resp.metrics.get('prompt_tokens', 'N/A')
        ct = resp.metrics.get('completion_tokens', 'N/A')
        print(f'Prompt tokens: {pt}')
        print(f'Completion tokens: {ct}')
    
    # Validate
    try:
        validated = ExtractionOutput.model_validate_json(content)
        print()
        print('[VALIDATION SUCCESS]')
        print(f'  Entities found: {len(validated.entities)}')
        print(f'  Rejected: {len(validated.rejected)}')
        print(f'  Page has ownership language: {validated.page_has_ownership_language}')
        print(f'  Agent confidence: {validated.agent_confidence}')
        print()
        print('=== PASS: Gemini extraction succeeded ===')
    except Exception as ve:
        print(f'[VALIDATION ERROR] {str(ve)[:200]}')
        print()
        print('=== FAIL: Validation failed ===')
        
except Exception as exc:
    total_time = time.perf_counter() - t0
    print(f'[ERROR] {type(exc).__name__}: {str(exc)[:500]}')
    print(f'Time: {total_time:.3f}s')
    print()
    print('=== FAIL: Test failed ===')
