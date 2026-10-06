"""M8: Anthropic and OpenAI-compatible adapters, exercised through the real gateway/runner with a fake transport."""
from __future__ import annotations

import json
import logging

import pytest

from corpdisc.agents.gateway import BreakerState
from corpdisc.agents.ports import (
    Message,
    ProviderAuthError,
    ProviderNotImplementedError,
    ProviderRequest,
    ProviderTransportError,
    ToolDescriptor,
)
from corpdisc.agents.providers import UnconfiguredProvider, build_provider_from_settings
from corpdisc.agents.providers.anthropic import AnthropicProvider
from corpdisc.agents.providers.cassette import ReplayProvider
from corpdisc.agents.providers.openai_compat import OpenAICompatProvider
from corpdisc.config import Settings
from corpdisc.contracts.agents import CallStatus, FailureKind
from corpdisc.transport import HttpReply, TransportFailure
from tests.agents.harness import build
from tests.agents.toy_agents import ToyEchoAgent

KEY = "sk-test-SECRETSECRET123456"
OUT = {"answer": "hi", "score": 0.5}
ECHO = ToyEchoAgent()


class FakeTransport:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def post(self, url, *, headers, body, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "body": json.loads(body), "timeout": timeout})
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(r, Exception):
            raise r
        return r


def reply(status=200, body=None, headers=None):
    return HttpReply(status, headers or {}, body if isinstance(body, str) else json.dumps(body or {}))


def anthropic_ok(text=json.dumps(OUT), tin=100, tout=50, extra=()):
    return reply(200, {"model": "claude-x", "content": [{"type": "text", "text": text}, *extra],
                       "usage": {"input_tokens": tin, "output_tokens": tout}})


def openai_ok(content=json.dumps(OUT), calls=None, tin=100, tout=50):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = calls
    return reply(200, {"model": "gpt-x", "choices": [{"message": msg}], "usage": {"prompt_tokens": tin, "completion_tokens": tout}})


def req(**kw):
    base = dict(model="m", system="SYS", messages=(Message("user", "hello"),), response_schema={"type": "object"},
                schema_name="EchoOut", temperature=0.0, max_output_tokens=200)
    base.update(kw)
    return ProviderRequest(**base)


def anth(*replies, **kw):
    t = FakeTransport(*replies)
    return AnthropicProvider(api_key=KEY, transport=t, **kw), t


def oai(*replies, **kw):
    t = FakeTransport(*replies)
    return OpenAICompatProvider(api_key=KEY, transport=t, **kw), t


# ------------------------------------------------------------------ request construction
def test_anthropic_request_shape_and_response_parsing():
    p, t = anth(anthropic_ok("```json\n" + json.dumps(OUT) + "\n```"), price_in_per_mtok=3.0, price_out_per_mtok=15.0, timeout=7)
    tools = (ToolDescriptor("get_page_excerpt", "read", {"type": "object"}),)
    msgs = (Message("user", "a"), Message("assistant", "b"), Message("tool", "RESULT"), Message("user", "repair"))
    r = p.complete(req(messages=msgs, tools=tools, temperature=0.2))
    c = t.calls[0]
    assert c["url"] == "https://api.anthropic.com/v1/messages" and c["timeout"] == 7
    assert c["headers"]["x-api-key"] == KEY and c["headers"]["anthropic-version"] == "2023-06-01"
    b = c["body"]
    assert (b["model"], b["max_tokens"], b["temperature"]) == ("m", 200, 0.2)
    assert b["system"].startswith("SYS") and '"type":"object"' in b["system"]  # schema is in the system prompt
    assert [m["role"] for m in b["messages"]] == ["user", "assistant", "user"]  # tool+user merged, roles alternate
    assert "RESULT" in b["messages"][2]["content"] and b["tools"][0]["name"] == "get_page_excerpt"
    assert json.loads(r.content) == OUT  # fences stripped
    assert (r.usage.tokens_in, r.usage.tokens_out) == (100, 50) and r.usage.cost_usd == pytest.approx(0.00105)


def test_anthropic_tool_use_blocks_become_tool_calls():
    block = {"type": "tool_use", "id": "tu1", "name": "get_page_excerpt", "input": {"page_id": 3}}
    p, _ = anth(reply(200, {"content": [block], "usage": {}}))
    r = p.complete(req())
    assert r.content is None and r.tool_calls[0].name == "get_page_excerpt" and r.tool_calls[0].arguments == {"page_id": 3}


def test_openai_request_shape_and_response_parsing():
    call = {"id": "c1", "function": {"name": "get_page_excerpt", "arguments": json.dumps({"page_id": 2})}}
    p, t = oai(openai_ok(calls=[call, {"id": "c2", "function": {"name": "x", "arguments": "{broken"}}]),
               base_url="https://llm.example.test/", price_in_per_mtok=1.0, price_out_per_mtok=2.0)
    r = p.complete(req(tools=(ToolDescriptor("get_page_excerpt", "read", {"type": "object"}),)))
    c = t.calls[0]
    assert c["url"] == "https://llm.example.test/v1/chat/completions" and c["headers"]["authorization"] == f"Bearer {KEY}"
    assert c["body"]["messages"][0] == {"role": "system", "content": "SYS"}
    assert c["body"]["response_format"]["type"] == "json_schema" and c["body"]["tools"][0]["type"] == "function"
    assert [x.arguments for x in r.tool_calls] == [{"page_id": 2}, {}]  # malformed tool arguments never crash
    assert r.usage.cost_usd == pytest.approx(0.0002)


# ------------------------------------------------------------------ error classification (both adapters)
@pytest.mark.parametrize("make", [anth, oai], ids=["anthropic", "openai"])
@pytest.mark.parametrize("status, kind", [(401, ProviderAuthError), (402, ProviderAuthError), (403, ProviderAuthError)])
def test_auth_and_quota_errors_are_not_retryable_and_never_leak_the_key(make, status, kind):
    p, _ = make(reply(status, {"error": {"message": f"bad key {KEY} for org"}}))
    with pytest.raises(kind) as e:
        p.complete(req())
    assert KEY not in str(e.value) and "[REDACTED]" in str(e.value)


@pytest.mark.parametrize("make", [anth, oai], ids=["anthropic", "openai"])
def test_transient_statuses_are_retryable_and_honour_retry_after(make):
    for status in (429, 500, 503, 529, 408):
        p, _ = make(reply(status, {}, {"retry-after": "7"}))
        with pytest.raises(ProviderTransportError) as e:
            p.complete(req())
        assert e.value.retryable and e.value.retry_after_s == 7.0
    p, _ = make(reply(400, {"error": {"message": "max_tokens too large"}}))
    with pytest.raises(ProviderTransportError) as e:
        p.complete(req())
    assert not e.value.retryable and "max_tokens" in str(e.value)


@pytest.mark.parametrize("make, ok_empty", [(anth, reply(200, {"content": [], "usage": {}})),
                                            (oai, reply(200, {"choices": [{"message": {"content": ""}}]}))])
def test_malformed_and_empty_responses(make, ok_empty):
    p, _ = make(reply(200, "<html>gateway error</html>"))
    with pytest.raises(ProviderTransportError, match="non-JSON") as e:
        p.complete(req())
    assert not e.value.retryable
    p, _ = make(reply(200, "[1,2,3]"))
    with pytest.raises(ProviderTransportError):
        p.complete(req())
    p, _ = make(ok_empty)
    with pytest.raises(ProviderTransportError, match="no content") as e:
        p.complete(req())
    assert e.value.retryable


@pytest.mark.parametrize("make", [anth, oai])
def test_network_failures_map_to_provider_errors(make):
    for code, retryable in (("TIMEOUT", True), ("CONNECTION", True), ("SSL", False), ("TOO_LARGE", False)):
        p, _ = make(TransportFailure(code))
        with pytest.raises(ProviderTransportError) as e:
            p.complete(req())
        assert e.value.retryable is retryable


def test_adapters_need_a_key_and_hide_it_in_repr():
    # Anthropic requires an API key
    with pytest.raises(ValueError):
        AnthropicProvider(api_key="", transport=FakeTransport())
    # OpenAI-compatible providers (including Ollama) can work without a key
    OpenAICompatProvider(api_key="", transport=FakeTransport())
    assert KEY not in repr(anth(anthropic_ok())[0]) and KEY not in repr(oai(openai_ok())[0])


# ------------------------------------------------------------------ through gateway + runner + breaker + budget
def wire(tmp_path, provider, **kw):
    s = build(tmp_path, [], **kw)
    s.runner._gw._provider = provider
    return s


def test_retry_after_is_honoured_then_success_and_usage_is_recorded(tmp_path):
    p, t = anth(reply(429, {}, {"retry-after": "3"}), anthropic_ok(), price_in_per_mtok=1000.0, price_out_per_mtok=1000.0)
    s = wire(tmp_path, p)
    r = s.runner.invoke(ECHO, s.req(ECHO))
    assert r.status is CallStatus.OK and len(t.calls) == 2 and s.clock.sleeps == [3.0]
    row = s.conn.execute("SELECT llm_tokens_used, llm_cost_usd FROM runs").fetchone()
    assert row[0] == 150 and row[1] == pytest.approx(0.15)
    assert json.loads(s.rows()[0]["response_json"])["transport"] == {"requests": 2, "errors": ["ProviderTransportError"]}


def test_auth_failure_opens_the_breaker_and_stops_all_provider_traffic(tmp_path):
    p, t = oai(reply(401, {"error": {"message": "invalid key"}}))
    s = wire(tmp_path, p)
    r = s.runner.invoke(ECHO, s.req(ECHO))
    assert r.error.kind is FailureKind.TRANSPORT and s.breaker.state is BreakerState.OPEN and len(t.calls) == 1
    again = s.runner.invoke(ECHO, s.req(ECHO, text="other"))
    assert again.error.kind is FailureKind.CIRCUIT_OPEN and len(t.calls) == 1


def test_truncated_json_goes_through_schema_repair(tmp_path):
    p, t = anth(anthropic_ok('{"answer": "hi", "sco'), anthropic_ok())
    s = wire(tmp_path, p)
    r = s.runner.invoke(ECHO, s.req(ECHO))
    assert r.status is CallStatus.OK and r.meta.attempt == 2
    assert [x["status"] for x in s.rows()] == ["FAILED", "OK"] and s.rows()[0]["failure_kind"] == "SCHEMA"
    assert "hello" not in t.calls[1]["body"]["messages"][-1]["content"]  # repair message carries errors only


def test_repeated_garbage_fails_closed_with_schema_error(tmp_path):
    p, _ = oai(openai_ok("definitely not json"))
    s = wire(tmp_path, p)
    r = s.runner.invoke(ECHO, s.req(ECHO))
    assert r.status is CallStatus.FAILED and r.error.kind is FailureKind.SCHEMA


def test_secret_never_reaches_logs_database_or_cassette(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    cassette = tmp_path / "rec.jsonl"
    st = Settings(_env_file=None, llm_provider="anthropic", llm_api_key=KEY, llm_record_to=cassette,
                  db_path=tmp_path / "x.db", cache_dir=tmp_path / "c", log_dir=tmp_path / "l")
    t = FakeTransport(reply(401, {"error": {"message": f"nope {KEY}"}}), anthropic_ok())
    p = build_provider_from_settings(st, transport=t)
    s = wire(tmp_path, p)
    s.runner.invoke(ECHO, s.req(ECHO))   # 401: fails
    s.breaker.record_success()
    s.runner.invoke(ECHO, s.req(ECHO, text="second"))
    blob = " ".join(str(v) for row in s.conn.execute("SELECT * FROM agent_invocations").fetchall() for v in tuple(row))
    blob += caplog.text + cassette.read_text(encoding="utf-8") + repr(st) + repr(p)
    assert KEY not in blob


def test_record_with_real_adapter_then_replay_without_transport(tmp_path):
    cassette = tmp_path / "default.jsonl"
    st = Settings(_env_file=None, llm_provider="openai", llm_api_key=KEY, llm_record_to=cassette,
                  db_path=tmp_path / "x.db", cache_dir=tmp_path / "c", log_dir=tmp_path / "l")
    rec = build_provider_from_settings(st, transport=FakeTransport(openai_ok()))
    s = wire(tmp_path / "a", rec)
    first = s.runner.invoke(ECHO, s.req(ECHO))
    replay = ReplayProvider(cassette)
    s2 = wire(tmp_path / "b", replay, cache=False)
    assert s2.runner.invoke(ECHO, s2.req(ECHO)).payload == first.payload
    miss = s2.runner.invoke(ECHO, s2.req(ECHO, text="never recorded"))
    assert miss.status is CallStatus.FAILED and "cassette miss" in miss.error.message  # fails closed, never live


# ------------------------------------------------------------------ factory from settings
def test_factory_from_settings(tmp_path):
    base = dict(_env_file=None, db_path=tmp_path / "x.db", cache_dir=tmp_path / "c", log_dir=tmp_path / "l")
    unconfigured = build_provider_from_settings(Settings(**base))
    assert isinstance(unconfigured, UnconfiguredProvider)
    with pytest.raises(ProviderAuthError):
        unconfigured.complete(req())
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        build_provider_from_settings(Settings(**base, llm_provider="anthropic"), transport=FakeTransport())
    with pytest.raises(ValueError, match="transport"):
        build_provider_from_settings(Settings(**base, llm_provider="openai", llm_api_key=KEY))
    with pytest.raises(ProviderNotImplementedError):
        build_provider_from_settings(Settings(**base, llm_provider="gemini"))
    with pytest.raises(ValueError, match="unknown"):
        build_provider_from_settings(Settings(**base, llm_provider="mystery"))
    assert build_provider_from_settings(Settings(**base, llm_provider="fake")).name == "fake"
    a = build_provider_from_settings(Settings(**base, llm_provider="anthropic", llm_api_key=KEY), transport=FakeTransport())
    assert a.name == "anthropic" and Settings(**base, llm_record_to="").llm_record_to is None
