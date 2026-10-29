"""Groq provider tests: call provider.complete with a recording fake transport and inspect the POSTed body."""
from __future__ import annotations

import json

import pytest
from pydantic import SecretStr

from corpdisc.agents.ports import Message, ProviderAuthError, ProviderRequest, ProviderTransportError, ToolDescriptor
from corpdisc.agents.providers import GroqProvider, build_provider_from_settings
from corpdisc.agents.providers.http_common import ratelimit_metrics, retry_after
from corpdisc.config import Settings
from corpdisc.contracts.agent_io import RelationshipOutput
from corpdisc.transport import HttpReply, TransportFailure

SCHEMA = RelationshipOutput.model_json_schema()


def ok_reply(content: str | None = '{"assessments": []}', *, tool_calls=None, headers=None) -> HttpReply:
    msg: dict = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    body = {"model": "m", "choices": [{"message": msg, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    return HttpReply(200, headers or {}, json.dumps(body))


class RecordingTransport:
    def __init__(self, *replies: HttpReply | TransportFailure) -> None:
        self.replies = list(replies)
        self.posts: list[dict] = []

    def post(self, url, *, headers, body, timeout):  # noqa: ANN001
        self.posts.append({"url": url, "headers": dict(headers), "body": json.loads(body)})
        r = self.replies.pop(0)
        if isinstance(r, TransportFailure):
            raise r
        return r


def request(*, tools=(), reasoning=None) -> ProviderRequest:
    return ProviderRequest(model="m", system="SYS", messages=(Message("user", "hi"),), response_schema=SCHEMA,
                           schema_name="RelationshipOutput", temperature=0.0, max_output_tokens=200, tools=tools,
                           reasoning=reasoning)


def provider(transport, model="llama-3.3-70b-versatile") -> GroqProvider:
    return GroqProvider(api_key="test-key-12345", transport=transport, model_name=model)


TOOL = ToolDescriptor("get_page_excerpt", "Read a page", {"type": "object", "properties": {}})


def test_no_tools_uses_json_schema_and_schema_in_prompt():
    t = RecordingTransport(ok_reply())
    provider(t).complete(request())
    body = t.posts[0]["body"]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert "tools" not in body
    system = body["messages"][0]["content"]
    assert system.startswith("SYS")
    assert '"assessments"' in system  # the required field name is visible to the model


def test_tools_sent_without_response_format_but_schema_still_in_prompt():
    t = RecordingTransport(ok_reply())
    provider(t).complete(request(tools=(TOOL,)))
    body = t.posts[0]["body"]
    assert body["tools"][0]["function"]["name"] == "get_page_excerpt"
    assert "response_format" not in body
    assert '"assessments"' in body["messages"][0]["content"]


def test_400_on_json_schema_falls_back_to_json_object_once():
    bad = HttpReply(400, {}, json.dumps({"error": {"message": "response_format json_schema is not supported"}}))
    t = RecordingTransport(bad, ok_reply())
    resp = provider(t).complete(request())
    assert [p["body"]["response_format"]["type"] for p in t.posts] == ["json_schema", "json_object"]
    assert resp.content == '{"assessments": []}'
    assert '"assessments"' in t.posts[1]["body"]["messages"][0]["content"]


def test_unrelated_400_is_not_retried_and_is_not_retryable():
    t = RecordingTransport(HttpReply(400, {}, json.dumps({"error": {"message": "bad temperature"}})))
    with pytest.raises(ProviderTransportError) as ei:
        provider(t).complete(request())
    assert not ei.value.retryable
    assert len(t.posts) == 1


def test_tool_calls_parsed():
    calls = [{"id": "c1", "function": {"name": "get_page_excerpt", "arguments": '{"page_id": 3}'}}]
    t = RecordingTransport(ok_reply(None, tool_calls=calls))
    resp = provider(t).complete(request(tools=(TOOL,)))
    assert resp.content is None
    assert resp.tool_calls[0].name == "get_page_excerpt"
    assert resp.tool_calls[0].arguments == {"page_id": 3}


def test_code_fences_stripped():
    t = RecordingTransport(ok_reply('```json\n{"assessments": []}\n```'))
    assert provider(t).complete(request()).content == '{"assessments": []}'


def test_qwen3_reasoning_none_sets_reasoning_effort_and_retries_without_if_rejected():
    rejected = HttpReply(400, {}, json.dumps({"error": {"message": "reasoning_effort is not supported"}}))
    t = RecordingTransport(rejected, ok_reply())
    provider(t, model="qwen/qwen3-32b").complete(request(reasoning="none"))
    assert t.posts[0]["body"]["reasoning_effort"] == "none"
    assert "reasoning_effort" not in t.posts[1]["body"]


def test_reasoning_effort_not_sent_for_other_models():
    t = RecordingTransport(ok_reply())
    provider(t).complete(request(reasoning="none"))
    assert "reasoning_effort" not in t.posts[0]["body"]


def test_429_is_retryable_with_retry_after_and_ratelimit_headers_parsed():
    t = RecordingTransport(HttpReply(429, {"retry-after": "45"}, "{}"))
    with pytest.raises(ProviderTransportError) as ei:
        provider(t).complete(request())
    assert ei.value.retryable and ei.value.retry_after_s == 45.0
    assert retry_after({"retry-after": "2m59.5s"}) == pytest.approx(179.5)
    m = ratelimit_metrics({"x-ratelimit-remaining-tokens": "900", "x-ratelimit-reset-tokens": "1.5s",
                           "x-ratelimit-remaining-requests": "7", "x-ratelimit-reset-requests": "2m0s"})
    assert m == {"rl_remaining_tokens": 900, "rl_reset_tokens_s": 1.5, "rl_remaining_requests": 7,
                 "rl_reset_requests_s": 120.0}


def test_success_metrics_carry_ratelimit_headers():
    t = RecordingTransport(ok_reply(headers={"x-ratelimit-remaining-tokens": "500"}))
    assert provider(t).complete(request()).metrics["rl_remaining_tokens"] == 500


def test_401_is_auth_error():
    t = RecordingTransport(HttpReply(401, {}, "{}"))
    with pytest.raises(ProviderAuthError):
        provider(t).complete(request())


def test_key_required_and_masked():
    with pytest.raises(ValueError, match="GROQ_API_KEY is required"):
        GroqProvider(api_key="", transport=RecordingTransport())
    assert "test-key" not in repr(provider(RecordingTransport()))


def test_provider_from_settings():
    base = dict(_env_file=None, db_path="x.db", cache_dir="c", log_dir="l")
    s = Settings(**base, llm_provider="groq", groq_api_key=SecretStr("k"), groq_model="llama-3.3-70b-versatile")
    assert build_provider_from_settings(s, transport=RecordingTransport()).name == "groq"
    with pytest.raises(ValueError, match="GROQ_API_KEY is required"):
        build_provider_from_settings(Settings(**base, llm_provider="groq", groq_api_key=None),
                                     transport=RecordingTransport())
