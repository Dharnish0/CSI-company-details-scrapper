"""The preflight script's core works against the strict Ollama fake (mechanics only; no performance claim)."""
from __future__ import annotations

import importlib.util
import json
import pathlib

from tests.agents.test_ollama_provider import StrictOllama

SPEC = importlib.util.spec_from_file_location("ollama_preflight", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "ollama_preflight.py")
pf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pf)
EMPTY = json.dumps({"entities": [], "rejected": [], "page_has_ownership_language": False, "agent_confidence": 0.5})


def test_run_case_reads_back_measured_facts_from_sqlite(tmp_path):
    fake = StrictOllama(EMPTY, thinking_tokens=2500)
    r = pf.run_case("B", 8, transport=fake, workdir=tmp_path, env_file=None, llm_extraction_reasoning="none")
    assert r["schema_valid"] and len(r["attempts"]) == 1 and r["entities"] == 1          # only the root: nothing invented
    a = r["attempts"][0]
    assert a["finish_reason"] == "stop" and a["request_bytes"] > 5000 and a["eval_ms"] > 0 and a["reasoning_chars"] == 0
    assert fake.calls[0]["body"]["think"] is False and fake.ignored == []


def test_default_thinking_is_visible_in_the_measurements(tmp_path):
    r = pf.run_case("B2", 8, transport=StrictOllama(EMPTY, thinking_tokens=2995), workdir=tmp_path, env_file=None, llm_extraction_reasoning="default")
    a = r["attempts"][0]
    assert a["reasoning_chars"] > 0 and a["finish_reason"] == "length" and not r["schema_valid"]   # thinking ate the 3000-token cap


def test_environment_reports_unreachable_ollama_instead_of_crashing():
    env = pf.environment("http://127.0.0.1:9", "qwen3:4b", timeout=1.0)
    assert "cannot reach Ollama" in env["error"] and "ram_gb" in env
