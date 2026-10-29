"""Unit tests for OpenAI provider with budget guard."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from corpdisc.agents.ports import (
    Message,
    ProviderRequest,
    ProviderResponse,
    ProviderTransportError,
    ProviderUsage,
    ToolDescriptor,
)
from corpdisc.agents.providers.openai import OpenAIProvider
from corpdisc.agents.providers.fake import FakeLLMProvider
from corpdisc.services.budget import BudgetGuard, BudgetLimits
from corpdisc.transport import JsonTransport, HttpReply, TransportFailure


class MockTransport(JsonTransport):
    """Mock transport that returns fake responses for testing."""

    def __init__(self, response_body: dict) -> None:
        self._response_body = response_body
        self.calls: list[dict] = []

    def post(self, url: str, headers: dict[str, str], body: str, timeout: float) -> HttpReply:
        self.calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        return HttpReply(status=200, headers={}, body=json.dumps(self._response_body))


@pytest.fixture
def mock_response():
    """Create a standard OpenAI-compatible response."""
    return {
        "id": "chatcmpl-test-123",
        "object": "chat.completion",
        "created": 1234567890,
        "model": "gpt-4.1-mini",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": '{"name": "Test Company", "description": "A test company"}',
                    "tool_calls": [],
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 150, "completion_tokens": 25, "total_tokens": 175},
    }


@pytest.fixture
def provider(mock_response, tmp_path):
    """Create an OpenAI provider with mock transport."""
    transport = MockTransport(mock_response)
    return OpenAIProvider(
        api_key="test-key-123",
        transport=transport,
        timeout=30.0,
        model_name="gpt-4.1-mini",
    )


class TestOpenAIProvider:
    """Tests for OpenAIProvider."""

    def test_init_requires_api_key(self):
        """Provider init fails without API key."""
        from corpdisc.transport import JsonTransport

        class DummyTransport(JsonTransport):
            def post(self, *args, **kwargs):
                return HttpReply(status=200, headers={}, body="{}")

        with pytest.raises(ValueError, match="OPENAI_API_KEY is required"):
            OpenAIProvider(api_key="", transport=DummyTransport())

    def test_init_uses_default_model(self, mock_response):
        """Provider uses gpt-4.1-mini as default model."""
        transport = MockTransport(mock_response)
        provider = OpenAIProvider(api_key="test-key", transport=transport)
        assert provider._model_name == "gpt-4.1-mini"
        assert provider._pin == 0.40  # Input price per M token
        assert provider._pout == 1.60  # Output price per M token

    def test_complete_returns_response(self, provider, mock_response):
        """Provider.complete returns a proper ProviderResponse."""
        request = ProviderRequest(
            model="gpt-4.1-mini",
            system="You are a helpful assistant.",
            messages=(Message(role="user", content="Test message"),),
            schema_name="CompanySchema",
            response_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}, "description": {"type": "string"}},
            },
            max_output_tokens=512,
            temperature=0.0,
            tools=(),
        )

        response = provider.complete(request)

        assert isinstance(response, ProviderResponse)
        assert response.content == '{"name": "Test Company", "description": "A test company"}'
        assert response.usage.tokens_in == 150
        assert response.usage.tokens_out == 25
        assert response.model == "gpt-4.1-mini"
        assert response.finish_reason == "stop"

    def test_complete_calculates_cost(self, provider, mock_response):
        """Provider calculates cost from usage and pricing."""
        request = ProviderRequest(
            model="gpt-4.1-mini",
            system="Test system",
            messages=(),
            schema_name="TestSchema",
            response_schema={"type": "object", "properties": {}},
            max_output_tokens=512,
            temperature=0.0,
            tools=(),
        )

        response = provider.complete(request)

        # Cost = (prompt_tokens * $0.40 + completion_tokens * $1.60) / 1,000,000
        expected_cost = (150 * 0.40 + 25 * 1.60) / 1_000_000
        assert response.usage.cost_usd == pytest.approx(expected_cost, rel=1e-6)

    def test_complete_with_tools(self, mock_response, tmp_path):
        """Provider works with tools parameter."""
        transport = MockTransport(mock_response)
        provider = OpenAIProvider(api_key="test-key", transport=transport)

        tools = [
            ToolDescriptor(
                name="lookup_company",
                description="Look up company information",
                args_schema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            )
        ]

        request = ProviderRequest(
            model="gpt-4.1-mini",
            system="You can use tools to help users.",
            messages=(Message(role="user", content="Find info about Acme Corp"),),
            schema_name="ResponseSchema",
            response_schema={"type": "object", "properties": {}},
            max_output_tokens=512,
            temperature=0.0,
            tools=tuple(tools),
        )

        response = provider.complete(request)

        assert isinstance(response, ProviderResponse)
        assert len(transport.calls) == 1
        body = json.loads(transport.calls[0]["body"])
        assert "tools" in body
        assert body["tools"][0]["function"]["name"] == "lookup_company"

    def test_complete_handles_tool_calls(self, tmp_path):
        """Provider extracts tool calls from response."""
        tool_call_response = {
            "id": "chatcmpl-test-456",
            "object": "chat.completion",
            "created": 1234567890,
            "model": "gpt-4.1-mini",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {
                                    "name": "lookup_company",
                                    "arguments": '{"name": "Acme Corp"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        }

        transport = MockTransport(tool_call_response)
        provider = OpenAIProvider(api_key="test-key", transport=transport)

        request = ProviderRequest(
            model="gpt-4.1-mini",
            system="Use tools to help.",
            messages=(),
            schema_name="TestSchema",
            response_schema={"type": "object", "properties": {}},
            max_output_tokens=512,
            temperature=0.0,
            tools=tuple([
                ToolDescriptor(
                    name="lookup_company",
                    description="Look up company",
                    args_schema={"type": "object", "properties": {"name": {"type": "string"}}},
                )
            ]),
        )

        response = provider.complete(request)

        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "lookup_company"
        assert response.tool_calls[0].arguments == {"name": "Acme Corp"}
        assert response.content is None

    def test_complete_raises_on_empty_response(self, tmp_path):
        """Provider raises error when no content or tool calls."""
        empty_response = {
            "id": "chatcmpl-empty",
            "choices": [{"message": {"role": "assistant", "content": ""}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 0},
        }

        transport = MockTransport(empty_response)
        provider = OpenAIProvider(api_key="test-key", transport=transport)

        request = ProviderRequest(
            model="gpt-4.1-mini",
            system="Test",
            messages=(),
            schema_name="Test",
            response_schema={"type": "object", "properties": {}},
            max_output_tokens=512,
            temperature=0.0,
            tools=(),
        )

        with pytest.raises(ProviderTransportError, match="openai returned no content"):
            provider.complete(request)

    def test_complete_uses_correct_model_in_request(self, provider, mock_response):
        """Provider sends correct model name in API request."""
        request = ProviderRequest(
            model="gpt-4.1-mini",
            system="Test",
            messages=(),
            schema_name="Test",
            response_schema={"type": "object", "properties": {}},
            max_output_tokens=512,
            temperature=0.0,
            tools=(),
        )

        provider.complete(request)

        assert len(provider._t.calls) == 1
        body = json.loads(provider._t.calls[0]["body"])
        assert body["model"] == "gpt-4.1-mini"

    def test_complete_uses_custom_model(self, mock_response, tmp_path):
        """Provider uses custom model if specified."""
        custom_response = {
            "id": "chatcmpl-custom",
            "choices": [{"message": {"content": '{"test": true}', "tool_calls": []}}],
            "model": "gpt-4.5-preview",
            "usage": {"prompt_tokens": 50, "completion_tokens": 10},
        }

        transport = MockTransport(custom_response)
        provider = OpenAIProvider(
            api_key="test-key",
            transport=transport,
            model_name="gpt-4.5-preview",
        )

        request = ProviderRequest(
            model="gpt-4.5-preview",
            system="Test",
            messages=(),
            schema_name="Test",
            response_schema={"type": "object", "properties": {}},
            max_output_tokens=512,
            temperature=0.0,
            tools=(),
        )

        response = provider.complete(request)

        assert response.model == "gpt-4.5-preview"
        body = json.loads(transport.calls[0]["body"])
        assert body["model"] == "gpt-4.5-preview"

    def test_low_cost_of_gpt41mini(self):
        """Verify gpt-4.1-mini is affordable for testing."""
        mock_response = {
            "id": "chatcmpl-test-123",
            "choices": [{"message": {"content": '{"name": "Test"}', "tool_calls": []}}],
            "model": "gpt-4.1-mini",
            "usage": {"prompt_tokens": 150, "completion_tokens": 25},
        }
        transport = MockTransport(mock_response)
        provider = OpenAIProvider(api_key="test-key", transport=transport)

        # Run 100 extraction calls
        request = ProviderRequest(
            model="gpt-4.1-mini",
            system="Extract company data.",
            messages=(),
            schema_name="CompanySchema",
            response_schema={"type": "object", "properties": {}},
            max_output_tokens=200,
            temperature=0.0,
            tools=(),
        )

        total_cost = 0.0
        for _ in range(100):
            response = provider.complete(request)
            total_cost += response.usage.cost_usd

        # 100 calls x ~$0.000094 = ~$0.0094, well under $0.10
        assert total_cost < 0.05  # Much less than $0.10 limit
