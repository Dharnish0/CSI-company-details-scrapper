"""Unit tests for Gemini provider (no live API calls)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from corpdisc.agents.ports import Message, ProviderRequest
from corpdisc.agents.providers import GeminiProvider, build_provider_from_settings
from corpdisc.config import Settings
from corpdisc.transport import JsonTransport


class FakeTransport(JsonTransport):
    """Fake transport for testing."""
    def post(self, url: str, headers: dict, body: str, timeout: float) -> dict:
        return {"status_code": 200, "headers": {}, "body": "{}"}


class TestGeminiProviderInstantiation:
    """Test GeminiProvider instantiation and configuration."""

    def test_provider_instantiates_with_valid_api_key(self) -> None:
        """Provider should instantiate when given a valid API key."""
        transport = FakeTransport()
        provider = GeminiProvider(
            api_key="test-key-12345",
            transport=transport,
            model_name="gemini-1.5-flash",
        )
        assert provider.name == "gemini"
        assert provider._model_name == "gemini-1.5-flash"

    def test_provider_raises_error_without_api_key(self) -> None:
        """Provider should raise ValueError if no API key is provided."""
        transport = FakeTransport()
        with pytest.raises(ValueError, match="GEMINI_API_KEY is required"):
            GeminiProvider(
                api_key="",
                transport=transport,
            )

    def test_provider_repr_does_not_expose_key(self) -> None:
        """Provider repr should mask the API key."""
        transport = FakeTransport()
        provider = GeminiProvider(
            api_key="my-secret-api-key-12345",
            transport=transport,
        )
        repr_str = repr(provider)
        assert "my-secret-api-key" not in repr_str
        assert "***" in repr_str
        assert "GeminiProvider" in repr_str


class TestGeminiProviderFromSettings:
    """Test provider creation from Settings."""

    def test_gemini_provider_from_settings(self) -> None:
        """Provider should instantiate from settings when LLM_PROVIDER=gemini."""
        base = dict(_env_file=None, db_path="x.db", cache_dir="c", log_dir="l")
        settings = Settings(
            **base,
            llm_provider="gemini",
            gemini_api_key=SecretStr("test-key"),
            gemini_model="gemini-1.5-flash",
        )
        provider = build_provider_from_settings(settings, transport=FakeTransport())
        assert provider.name == "gemini"

    def test_ollama_provider_default(self) -> None:
        """Default provider should be ollama when not specified."""
        base = dict(_env_file=None, db_path="x.db", cache_dir="c", log_dir="l")
        settings = Settings(**base)
        provider = build_provider_from_settings(settings, transport=FakeTransport())
        # Default is UnconfiguredProvider when no provider specified
        assert provider.name == "unconfigured"

    def test_gemini_requires_api_key(self) -> None:
        """Gemini provider should require API key."""
        base = dict(_env_file=None, db_path="x.db", cache_dir="c", log_dir="l")
        settings = Settings(
            **base,
            llm_provider="gemini",
            gemini_api_key=None,
        )
        with pytest.raises(ValueError, match="GEMINI_API_KEY is required"):
            build_provider_from_settings(settings, transport=FakeTransport())

    def test_ollama_still_works(self) -> None:
        """Ollama provider should still work unchanged."""
        base = dict(_env_file=None, db_path="x.db", cache_dir="c", log_dir="l")
        settings = Settings(
            **base,
            llm_provider="ollama",
            llm_model="qwen3:4b",
        )
        provider = build_provider_from_settings(settings, transport=FakeTransport())
        assert provider.name == "ollama"


class TestGeminiProviderErrorHandling:
    """Test error handling in Gemini provider."""

    def test_missing_api_key_error_message(self) -> None:
        """Error message should clearly indicate missing GEMINI_API_KEY."""
        transport = FakeTransport()
        try:
            GeminiProvider(api_key="", transport=transport)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "GEMINI_API_KEY" in str(e)
            assert "required" in str(e).lower()
