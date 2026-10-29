"""Per-run provider/model override: the model lands on the field of the selected provider, other settings survive."""
from __future__ import annotations

from pydantic import SecretStr

from corpdisc.app import provider_settings_for_run
from corpdisc.agents.providers import build_provider_from_settings
from corpdisc.config import Settings

BASE = dict(_env_file=None, db_path="x.db", cache_dir="c", log_dir="l", groq_model="groq-default",
            gemini_model="gemini-default", llm_model="ollama-default", groq_api_key=SecretStr("g"),
            gemini_api_key=SecretStr("m"), gemini_thinking="low", llm_rpm_limit=7)


class T:
    def post(self, *a, **k):
        raise AssertionError("no network")


def test_gemini_override_changes_gemini_model_only():
    s = provider_settings_for_run(Settings(**BASE), run_provider="gemini", run_model="gemini-x")
    assert (s.gemini_model, s.groq_model, s.llm_model) == ("gemini-x", "groq-default", "ollama-default")
    assert s.llm_provider == "gemini" and s.gemini_thinking == "low" and s.llm_rpm_limit == 7  # nothing dropped
    assert build_provider_from_settings(s, transport=T())._model_name == "gemini-x"


def test_groq_override_changes_groq_model_only():
    s = provider_settings_for_run(Settings(**BASE), run_provider="groq", run_model="openai/gpt-oss-120b")
    assert (s.groq_model, s.gemini_model) == ("openai/gpt-oss-120b", "gemini-default")
    assert build_provider_from_settings(s, transport=T())._model_name == "openai/gpt-oss-120b"


def test_ollama_override_uses_llm_model():
    s = provider_settings_for_run(Settings(**BASE), run_provider="ollama", run_model="mistral:latest")
    assert s.llm_model == "mistral:latest" and s.groq_model == "groq-default"


def test_no_model_override_keeps_configured_models():
    s = provider_settings_for_run(Settings(**BASE), run_provider="groq", run_model=None)
    assert s.groq_model == "groq-default" and s.llm_provider == "groq"


def test_pacer_defaults_follow_provider_and_settings():
    from corpdisc.app import make_pacer
    from tests.helpers.clock import FakeClock

    c = FakeClock()
    assert make_pacer(Settings(**BASE | {"llm_rpm_limit": -1}), "groq", c, c).rpm == 30
    assert make_pacer(Settings(**BASE | {"llm_rpm_limit": -1}), "gemini", c, c) is None
    assert make_pacer(Settings(**BASE | {"llm_rpm_limit": 12}), "ollama", c, c).rpm == 12
    assert make_pacer(Settings(**BASE | {"llm_rpm_limit": 0}), "groq", c, c) is None
