"""Model registry reflects reality: Ollama models are listed only when the server reports them installed."""
from __future__ import annotations

from pydantic import SecretStr

from corpdisc.config import Settings
from corpdisc.llm_registry import (
    OllamaProbe,
    build_registry,
    discover_ollama,
    format_registry,
    ollama_base_url,
    ollama_model_installed,
)

BASE = dict(_env_file=None, db_path="x.db", cache_dir="c", log_dir="l")


def settings(**kw) -> Settings:
    return Settings(**BASE, **kw)


def tags(models):
    return lambda url, timeout: (200, {"models": [{"name": m, "model": m} for m in models]})


def ollama_entries(reg):
    return [m for m in reg.models if m.provider_id == "ollama"]


def test_discover_lists_installed_models_from_api_tags():
    seen = {}

    def fetch(url, timeout):
        seen.update(url=url, timeout=timeout)
        return 200, {"models": [{"name": "mistral:latest"}, {"model": "qwen3:4b"}, {"name": "mistral:latest"}]}

    probe = discover_ollama("http://localhost:11434/", fetch=fetch)
    assert probe.reachable and probe.models == ("mistral:latest", "qwen3:4b")
    assert seen["url"] == "http://localhost:11434/api/tags" and seen["timeout"] <= 5  # short timeout


def test_discover_marks_server_unreachable_on_errors_and_bad_responses():
    def boom(url, timeout):
        raise ConnectionError("refused")

    assert discover_ollama(fetch=boom) == OllamaProbe(False, (), "ConnectionError", "http://localhost:11434")
    assert not discover_ollama(fetch=lambda u, t: (500, None)).reachable
    assert not discover_ollama(fetch=lambda u, t: (200, ["not", "a", "dict"])).reachable


def test_only_installed_models_are_listed_and_available():
    reg = build_registry(settings(), ollama_probe=discover_ollama(fetch=tags(["mistral:latest", "qwen3:4b"])))
    assert [(m.model_name, m.is_available) for m in ollama_entries(reg)] == [("mistral:latest", True), ("qwen3:4b", True)]
    assert "qwen3:8b" not in [m.model_name for m in reg.models]  # no hardcoded model names


def test_unreachable_server_yields_one_unavailable_placeholder_and_nothing_selectable():
    reg = build_registry(settings(), ollama_probe=OllamaProbe(False, (), "ConnectError", "http://localhost:11434"))
    entries = ollama_entries(reg)
    assert len(entries) == 1 and not entries[0].is_available and "not reachable" in entries[0].availability_reason
    assert all(m.provider_id != "ollama" for m in reg.selectable())
    assert "UNREACHABLE" in format_registry(reg, show_unavailable=True)


def test_cloud_llm_model_never_becomes_an_ollama_entry():
    s = settings(llm_provider="gemini", llm_model="gemini-3.8-flash", gemini_api_key=SecretStr("k"))
    reg = build_registry(s, ollama_probe=OllamaProbe(True, ("mistral:latest",), "", "http://localhost:11434"))
    assert [m.model_name for m in ollama_entries(reg)] == ["mistral:latest"]


def test_configured_but_not_installed_ollama_model_is_listed_unavailable():
    s = settings(llm_provider="ollama", llm_model="qwen3:8b")
    reg = build_registry(s, ollama_probe=OllamaProbe(True, ("mistral:latest",), "", "http://localhost:11434"))
    missing = [m for m in ollama_entries(reg) if m.model_name == "qwen3:8b"]
    assert len(missing) == 1 and not missing[0].is_available and "ollama pull qwen3:8b" in missing[0].availability_reason
    assert "qwen3:8b" not in [m.model_name for m in reg.selectable()]


def test_server_with_no_models_has_an_unavailable_placeholder():
    reg = build_registry(settings(), ollama_probe=OllamaProbe(True, (), "", "http://localhost:11434"))
    assert [m.is_available for m in ollama_entries(reg)] == [False]


def test_cloud_providers_depend_on_keys_only():
    reg = build_registry(settings(groq_api_key=SecretStr("k")), ollama_probe=OllamaProbe(False))
    by = {m.provider_id: m for m in reg.models}
    assert by["groq"].is_available and not by["gemini"].is_available and "GEMINI_API_KEY" in by["gemini"].availability_reason


def test_format_numbers_match_selection_indices():
    reg = build_registry(settings(groq_api_key=SecretStr("k")), ollama_probe=discover_ollama(fetch=tags(["a:1", "b:2"])))
    out = format_registry(reg, show_unavailable=True)
    selectable = reg.selectable()
    assert [m.model_name for m in selectable] == ["openai/gpt-oss-120b", "a:1", "b:2"]
    for n, m in enumerate(selectable, start=1):
        assert reg.get_by_index(n) is m
        assert f"[{n}] {m.display_name}" in out
    assert "[-] Gemini" in out  # unavailable entries carry no selection number


def test_installed_check_and_base_url_precedence():
    p = OllamaProbe(True, ("mistral:latest", "qwen3:4b"))
    assert ollama_model_installed(p, "mistral") and ollama_model_installed(p, "qwen3:4b")
    assert not ollama_model_installed(p, "qwen3:8b") and not ollama_model_installed(p, "")
    assert ollama_base_url(settings(llm_provider="groq", llm_base_url="http://x:1")) == "http://localhost:11434"
    assert ollama_base_url(settings(llm_provider="ollama", llm_base_url="http://x:1")) == "http://x:1"
