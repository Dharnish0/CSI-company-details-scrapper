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


# ---------------------------------------------------------------- fake-client behaviour tests (no network)
from types import SimpleNamespace as NS  # noqa: E402

from google.genai import errors as genai_errors  # noqa: E402
from google.genai import types as gt  # noqa: E402

from corpdisc.agents.ports import ProviderAuthError, ProviderTransportError  # noqa: E402

SCHEMA = {"type": "object", "title": "T", "additionalProperties": False, "properties": {"a": {"type": "string", "title": "A"}}}


class FakeModels:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def make_provider(*outcomes, model="gemini-3.8-flash", thinking="off"):
    models = FakeModels(*outcomes)
    p = GeminiProvider(api_key="k", model_name=model, thinking=thinking, client=NS(models=models))
    return p, models


def response(parts=None, *, finish="STOP", prompt=10, answer=5, thoughts=None, none_counts=False):
    parts = parts if parts is not None else [NS(text='{"a": "x"}', thought=False)]
    usage = NS(prompt_token_count=None if none_counts else prompt, candidates_token_count=None if none_counts else answer,
               thoughts_token_count=thoughts)
    return NS(candidates=[NS(content=NS(parts=parts), finish_reason=getattr(gt.FinishReason, finish))], usage_metadata=usage)


def req(**kw):
    base = dict(model="m", system="SYS", messages=(Message("user", "hi"),), response_schema=SCHEMA, schema_name="T",
                temperature=0.0, max_output_tokens=1000)
    base.update(kw)
    return ProviderRequest(**base)


class TestGeminiRequestConfig:
    def test_reasoning_none_turns_thinking_off_level_style(self) -> None:
        p, m = make_provider(response(), thinking="default")
        p.complete(req(reasoning="none"))
        cfg = m.calls[0]["config"]
        assert cfg.thinking_config.thinking_level == gt.ThinkingLevel.MINIMAL
        assert cfg.thinking_config.thinking_budget is None
        assert cfg.response_mime_type == "application/json"
        assert cfg.response_json_schema == {"type": "object", "properties": {"a": {"type": "string"}}}
        assert cfg.automatic_function_calling.disable is True
        assert cfg.max_output_tokens == 1000 + 256

    def test_budget_style_for_gemini_2_models(self) -> None:
        p, m = make_provider(response(), model="gemini-2.5-flash")
        p.complete(req())
        assert m.calls[0]["config"].thinking_config.thinking_budget == 0
        assert m.calls[0]["config"].max_output_tokens == 1000

    def test_default_mode_sends_no_thinking_config_but_raises_cap(self) -> None:
        p, m = make_provider(response(), thinking="default")
        p.complete(req())
        cfg = m.calls[0]["config"]
        assert cfg.thinking_config is None and cfg.max_output_tokens == 1000 + 4096

    def test_reasoning_default_overrides_off_setting(self) -> None:
        p, m = make_provider(response(), thinking="off")
        p.complete(req(reasoning="default"))
        assert m.calls[0]["config"].thinking_config is None

    def test_roles_are_mapped_and_merged(self) -> None:
        p, m = make_provider(response())
        p.complete(req(messages=(Message("user", "a"), Message("assistant", "b"), Message("tool", "c"))))
        assert [c.role for c in m.calls[0]["contents"]] == ["user", "model", "user"]

    def test_400_on_thinking_setting_retries_once_with_lowest_level(self) -> None:
        bad = genai_errors.ClientError(400, {"error": {"message": "Thinking level not supported for this model"}})
        p, m = make_provider(bad, response())
        assert p.complete(req()).content == '{"a": "x"}'
        assert len(m.calls) == 2
        assert m.calls[1]["config"].thinking_config.thinking_level == gt.ThinkingLevel.LOW


class TestGeminiResponseHandling:
    def test_thought_parts_are_excluded(self) -> None:
        parts = [NS(text="secret reasoning", thought=True), NS(text='{"a": "x"}', thought=False)]
        p, _ = make_provider(response(parts))
        assert p.complete(req()).content == '{"a": "x"}'

    def test_thought_tokens_are_counted_as_output(self) -> None:
        p, _ = make_provider(response(answer=5, thoughts=40))
        r = p.complete(req())
        assert r.usage.tokens_out == 45 and r.metrics["thoughts_tokens"] == 40 and r.metrics["completion_tokens"] == 5

    def test_none_token_counts_are_safe(self) -> None:
        p, _ = make_provider(response(none_counts=True))
        r = p.complete(req())
        assert (r.usage.tokens_in, r.usage.tokens_out) == (0, 0)

    @pytest.mark.parametrize(("finish", "expected"), [("STOP", "stop"), ("MAX_TOKENS", "length"), ("SAFETY", "content_filter"),
                                                      ("RECITATION", "content_filter"), ("OTHER", "other")])
    def test_finish_reason_mapping(self, finish: str, expected: str) -> None:
        p, _ = make_provider(response(finish=finish))
        assert p.complete(req()).finish_reason == expected

    def test_truncated_output_is_reported_as_length_not_as_valid(self) -> None:
        p, _ = make_provider(response([NS(text='{"a": "tru', thought=False)], finish="MAX_TOKENS", answer=1000))
        r = p.complete(req())
        assert r.finish_reason == "length" and r.content == '{"a": "tru'

    def test_empty_answer_with_stop_is_retryable(self) -> None:
        p, _ = make_provider(response([NS(text="", thought=False)]))
        with pytest.raises(ProviderTransportError) as ei:
            p.complete(req())
        assert ei.value.retryable

    def test_no_candidates_blocked_prompt_is_not_retryable(self) -> None:
        p, _ = make_provider(NS(candidates=[], prompt_feedback=NS(block_reason="SAFETY"), usage_metadata=None))
        with pytest.raises(ProviderTransportError) as ei:
            p.complete(req())
        assert not ei.value.retryable


class TestGeminiErrors:
    def test_429_is_retryable_rate_limited_with_delay(self) -> None:
        err = genai_errors.ClientError(429, {"error": {"message": "quota", "details": [{"retryDelay": "34s"}]}})
        p, _ = make_provider(err)
        with pytest.raises(ProviderTransportError) as ei:
            p.complete(req())
        assert ei.value.retryable and ei.value.rate_limited and ei.value.retry_after_s == 34.0

    def test_503_is_retryable(self) -> None:
        p, _ = make_provider(genai_errors.ServerError(503, {"error": {"message": "overloaded"}}))
        with pytest.raises(ProviderTransportError) as ei:
            p.complete(req())
        assert ei.value.retryable and not ei.value.rate_limited

    def test_401_is_an_auth_error(self) -> None:
        p, _ = make_provider(genai_errors.ClientError(401, {"error": {"message": "bad key"}}))
        with pytest.raises(ProviderAuthError):
            p.complete(req())

    def test_400_is_not_retryable(self) -> None:
        p, _ = make_provider(genai_errors.ClientError(400, {"error": {"message": "bad request"}}))
        with pytest.raises(ProviderTransportError) as ei:
            p.complete(req())
        assert not ei.value.retryable

    def test_unexpected_exception_does_not_leak_message(self) -> None:
        p, _ = make_provider(RuntimeError("contains secret-key-123"))
        with pytest.raises(ProviderTransportError) as ei:
            p.complete(req())
        assert "secret-key-123" not in str(ei.value)
