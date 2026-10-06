"""Provider factory (D16): fake, replay and record only. Real providers are refused."""

from __future__ import annotations

from pathlib import Path

from corpdisc.agents.ports import LLMProvider, ProviderAuthError, ProviderNotImplementedError
from corpdisc.agents.providers.anthropic import AnthropicProvider
from corpdisc.agents.providers.openai_compat import OpenAICompatProvider
from corpdisc.config import Settings
from corpdisc.transport import JsonTransport
from corpdisc.agents.providers.cassette import RecordingProvider, ReplayProvider
from corpdisc.agents.providers.fake import FakeLLMProvider

REAL_PROVIDERS = frozenset({"openai", "anthropic", "ollama"})
UNIMPLEMENTED_PROVIDERS = frozenset({"gemini", "google"})


def build_provider(name: str, *, cassette_path: Path | None = None, inner: LLMProvider | None = None) -> LLMProvider:
    key = (name or "").strip().lower()
    if key == "fake":
        return FakeLLMProvider()
    if key == "replay":
        if cassette_path is None:
            raise ValueError("replay needs cassette_path")
        return ReplayProvider(cassette_path)
    if key == "record":
        if cassette_path is None or inner is None:
            raise ValueError("record needs cassette_path and inner")
        return RecordingProvider(inner, cassette_path)
    if key in REAL_PROVIDERS:
        raise ValueError(f"provider '{key}' needs credentials and a transport: use build_provider_from_settings")
    if key in UNIMPLEMENTED_PROVIDERS:
        raise ProviderNotImplementedError(f"provider '{key}' is not implemented")
    raise ValueError(f"unknown provider {name!r}")


class UnconfiguredProvider:
    """Used when no LLM is configured: every call is an auth-class failure, so the circuit breaker opens at once and
    the Supervisor pauses the run with a clear message (25.4) instead of crashing."""

    name = "unconfigured"

    def complete(self, request):  # type: ignore[no-untyped-def]
        raise ProviderAuthError("no LLM provider is configured (set LLM_PROVIDER and LLM_API_KEY)")


def build_provider_from_settings(settings: Settings, *, transport: JsonTransport | None = None) -> LLMProvider:
    """Provider from configuration. Never reads or logs the key anywhere except to hand it to the adapter."""
    name = (settings.llm_provider or "").strip().lower()
    if name in ("", "none"):
        return UnconfiguredProvider()
    if name == "fake":
        return FakeLLMProvider()
    if name == "replay":
        return ReplayProvider(settings.llm_cassette_dir / "default.jsonl")
    inner: LLMProvider
    if name in REAL_PROVIDERS:
        if settings.llm_api_key is None and name != "ollama":
            raise ValueError(f"LLM_API_KEY is required for provider '{name}'")
        if transport is None:
            raise ValueError("a transport is required for a real provider")
        # Ollama uses localhost:11434 by default; other providers use their default base_url
        base_url = "http://localhost:11434" if name == "ollama" else (settings.llm_base_url or "")
        kw = dict(api_key=settings.llm_api_key.get_secret_value() if settings.llm_api_key else "", transport=transport, base_url=base_url,
                  timeout=settings.llm_timeout_seconds, price_in_per_mtok=settings.llm_price_in_per_mtok,
                  price_out_per_mtok=settings.llm_price_out_per_mtok)
        inner = AnthropicProvider(**kw) if name == "anthropic" else OpenAICompatProvider(**kw)
    elif name in UNIMPLEMENTED_PROVIDERS:
        raise ProviderNotImplementedError(f"provider '{name}' is not implemented")
    else:
        raise ValueError(f"unknown provider {settings.llm_provider!r}")
    if settings.llm_record_to is not None:
        return RecordingProvider(inner, settings.llm_record_to)
    return inner


__all__ = ["AnthropicProvider", "FakeLLMProvider", "OpenAICompatProvider", "RecordingProvider", "ReplayProvider",
           "UnconfiguredProvider", "build_provider", "build_provider_from_settings"]
