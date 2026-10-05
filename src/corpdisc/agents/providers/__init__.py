"""Provider factory (D16): fake, replay and record only. Real providers are refused."""

from __future__ import annotations

from pathlib import Path

from corpdisc.agents.ports import LLMProvider, ProviderNotImplementedError
from corpdisc.agents.providers.cassette import RecordingProvider, ReplayProvider
from corpdisc.agents.providers.fake import FakeLLMProvider

REAL_PROVIDERS = frozenset({"openai", "anthropic", "gemini", "google"})


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
        raise ProviderNotImplementedError(f"provider '{key}' is not implemented yet")
    raise ValueError(f"unknown provider {name!r}")


__all__ = ["FakeLLMProvider", "RecordingProvider", "ReplayProvider", "build_provider"]
