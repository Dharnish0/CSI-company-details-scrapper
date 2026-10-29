"""Native Ollama provider: bounded output, thinking control, context, diagnostics, errors, factory.

``StrictOllama`` models the server behaviour documented in Ollama's source (read on `main`, NOT recorded from a live server):
* /v1/chat/completions maps only ``max_tokens`` to ``num_predict`` (``max_completion_tokens`` is not a field) and, for a
  thinking-capable model with no reasoning field, thinking is ON; ``num_predict`` defaults to -1 (unlimited).
* /api/chat honours ``options.num_predict``, ``options.num_ctx`` and ``think``; ``done_reason`` is "length" at the cap.
Unknown request fields are silently ignored by the real server, so the fake RECORDS them: a field we send that the server
ignores is exactly the original defect.
"""
from __future__ import annotations

import json
import math

import pytest

from corpdisc.agents.ports import Message, ProviderRequest, ProviderTransportError, ToolDescriptor
from corpdisc.agents.providers import UnconfiguredProvider, build_provider_from_settings
from corpdisc.agents.providers.ollama import OllamaProvider, compact_schema
from corpdisc.agents.providers.openai_compat import OpenAICompatProvider
from corpdisc.config import Settings
from corpdisc.contracts.agents import AgentName, CallStatus, FailureKind
from corpdisc.transport import HttpReply, TransportFailure
from tests.agents.harness import build
from tests.agents.toy_agents import EchoOut, ToyEchoAgent

ECHO = ToyEchoAgent()
SCHEMA = EchoOut.model_json_schema()
RUNAWAY = 20_000  # what an unbounded generation would reach in this simulation


def intended(n_chars: int) -> str:
    return json.dumps({"answer": "x" * n_chars, "score": 0.5})


class StrictOllama:
    TOP = {"model", "messages", "stream", "format", "keep_alive", "tools", "options", "think"}
    OPTS = {"num_predict", "num_ctx", "temperature", "top_p", "top_k", "seed", "stop"}

    def __init__(self, text: str, *, thinking_tokens: int = 2500, thinking_capable: bool = True, tok_per_s: float = 10.0):
        self.text, self.think_tokens, self.capable, self.tps = text, thinking_tokens, thinking_capable, tok_per_s
        self.calls: list[dict] = []
        self.ignored: list[tuple[str, str]] = []
        self.mode = "ok"

    def post(self, url, *, headers, body, timeout):
        b = json.loads(body)
        self.calls.append({"url": url, "body": b, "headers": dict(headers), "timeout": timeout})
        if self.mode == "timeout":
            raise TransportFailure("TIMEOUT")
        if self.mode == "connection":
            raise TransportFailure("CONNECTION")
        if self.mode == "404":
            return HttpReply(404, {}, json.dumps({"error": "model 'qwen3:4b' not found, try pulling it first"}))
        if self.mode == "500":
            return HttpReply(500, {}, json.dumps({"error": "llama runner crashed"}))
        return self._native(b) if url.endswith("/api/chat") else self._compat(b)

    def _gen(self, num_predict, think, num_ctx, prompt_chars):
        pt = min(prompt_chars // 3, num_ctx)
        think_tok = self.think_tokens if (think and self.capable) else 0
        content_tok = math.ceil(len(self.text) / 4)
        limit = RUNAWAY if (num_predict is None or num_predict < 0) else num_predict
        limit = min(limit, max(num_ctx - pt, 0))
        total = think_tok + content_tok
        if total <= limit:
            return pt, total, think_tok, self.text, "stop"
        used = min(think_tok, limit)
        return pt, limit, used, self.text[: (limit - used) * 4], "length"

    def _native(self, b):
        self.ignored += [("top", k) for k in b if k not in self.TOP] + [("opt", k) for k in b.get("options", {}) if k not in self.OPTS]
        o = b.get("options", {})
        chars = sum(len(m["content"]) for m in b["messages"])
        pt, ct, th, content, why = self._gen(o.get("num_predict"), b.get("think", True), o.get("num_ctx", 4096), chars)
        msg = {"role": "assistant", "content": content}
        if th:
            msg["thinking"] = "t" * (th * 4)
        return HttpReply(200, {}, json.dumps({
            "model": b["model"], "message": msg, "done": True, "done_reason": why, "prompt_eval_count": pt, "eval_count": ct,
            "prompt_eval_duration": int(pt / 50 * 1e9), "eval_duration": int(ct / self.tps * 1e9), "load_duration": 500_000_000,
            "total_duration": int((pt / 50 + ct / self.tps + 0.5) * 1e9)}))

    def _compat(self, b):  # the /v1 shim: ONLY max_tokens is read
        self.ignored += [("top", k) for k in b if k in ("max_completion_tokens",)]
        chars = sum(len(m["content"]) for m in b["messages"])
        pt, ct, th, content, why = self._gen(b.get("max_tokens"), "reasoning_effort" not in b, 4096, chars)
        return HttpReply(200, {}, json.dumps({"model": b["model"], "choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": why}], "usage": {"prompt_tokens": pt, "completion_tokens": ct}}))


def req(cap=200, **kw):
    base = dict(model="qwen3:4b", system="SYS", messages=(Message("user", "hello"),), response_schema=SCHEMA, schema_name="EchoOut",
                temperature=0.0, max_output_tokens=cap)
    base.update(kw)
    return ProviderRequest(**base)


def ollama(fake, **kw):
    return OllamaProvider(transport=fake, **kw)


# ------------------------------------------------------------------ the original defect, and its fix, on the same server
def test_original_openai_compat_request_was_unbounded_on_ollama_but_native_is_bounded():
    fake = StrictOllama(intended(6000))                       # a long answer: ~1500 content tokens + 2500 thinking tokens
    old = OpenAICompatProvider(api_key="", transport=fake, base_url="http://localhost:11434").complete(req(cap=200))
    assert ("top", "max_completion_tokens") in fake.ignored   # the cap field the server never reads
    assert old.usage.tokens_out > 200                          # the app asked for 200; the server generated far more
    new = ollama(StrictOllama(intended(6000)), num_ctx=12288).complete(req(cap=200, reasoning="none"))
    assert new.usage.tokens_out == 200 and new.finish_reason == "length"   # a genuine, observable bound


def test_native_request_contains_no_field_the_server_ignores_and_sets_every_control():
    fake = StrictOllama(intended(40))
    ollama(fake, num_ctx=12288, keep_alive="30m").complete(req(cap=321, temperature=0.2, reasoning="none"))
    c = fake.calls[0]
    assert c["url"] == "http://localhost:11434/api/chat" and c["headers"] == {"content-type": "application/json"}
    assert fake.ignored == []
    b = c["body"]
    # REMOVED: format field - complex schemas cause Ollama grammar hangs in v0.9.6
    assert set(b) == {"model", "messages", "stream", "format", "keep_alive", "options", "think"}
    assert b["options"] == {"temperature": 0.2, "num_predict": 321, "num_ctx": 12288} and b["stream"] is False and b["think"] is False
    assert "max_tokens" not in b and "max_completion_tokens" not in b and "response_format" not in b
    assert b["format"] == SCHEMA  # the FULL schema is the grammar
    assert '"answer"' in b["messages"][0]["content"] and '"title"' not in b["messages"][0]["content"]  # compact copy in the prompt


def test_think_is_only_sent_when_a_reasoning_policy_exists():
    f1, f2, f3 = StrictOllama(intended(10)), StrictOllama(intended(10)), StrictOllama(intended(10))
    ollama(f1).complete(req())
    ollama(f2).complete(req(reasoning="none"))
    ollama(f3).complete(req(reasoning="default"))
    assert "think" not in f1.calls[0]["body"] and f2.calls[0]["body"]["think"] is False and f3.calls[0]["body"]["think"] is True


def test_default_thinking_eats_the_output_cap_which_is_why_extraction_turns_it_off():
    text = intended(1200)  # ~300 content tokens: fits a 3000 cap easily ...
    with_thinking = ollama(StrictOllama(text, thinking_tokens=2900)).complete(req(cap=3000))
    without = ollama(StrictOllama(text, thinking_tokens=2900)).complete(req(cap=3000, reasoning="none"))
    assert with_thinking.finish_reason == "length" and json.loads(without.content)["score"] == 0.5 and without.finish_reason == "stop"
    assert with_thinking.metrics["reasoning_chars"] > 0 and without.metrics["reasoning_chars"] == 0


# ------------------------------------------------------------------ truncation is surfaced and never "repaired"
def wire(tmp_path, fake, reasoning=None, **kw):
    s = build(tmp_path, [], **kw)
    s.runner.gateway._provider = ollama(fake, num_ctx=12288)
    s.runner._reasoning = {AgentName.DISCOVERY: reasoning} if reasoning else {}
    return s


def test_truncated_output_fails_without_a_repair_call_and_is_audited(tmp_path):
    fake = StrictOllama(intended(5000))
    s = wire(tmp_path, fake, reasoning="none")
    r = s.runner.invoke(ECHO, s.req(ECHO))
    assert r.status is CallStatus.FAILED and r.error.kind is FailureKind.SCHEMA and "finish_reason=length" in r.error.message
    assert len(fake.calls) == 1                                        # NO repair: it would resend the same input and truncate again
    row = s.rows()[0]
    resp = json.loads(row["response_json"])
    assert resp["finish_reason"] == "length" and resp["provider_metrics"]["completion_tokens"] == 200


def test_non_truncated_invalid_json_still_gets_exactly_one_repair(tmp_path):
    fake = StrictOllama("this is not json")
    s = wire(tmp_path, fake, reasoning="none")
    r = s.runner.invoke(ECHO, s.req(ECHO))
    assert r.status is CallStatus.FAILED and len(fake.calls) == 2      # initial + one repair, as before


def test_success_records_structured_diagnostics_but_no_page_content(tmp_path):
    fake = StrictOllama(intended(60), tok_per_s=8.0)
    s = wire(tmp_path, fake, reasoning="none")
    s.req(ECHO)
    r = s.runner.invoke(ECHO, s.req(ECHO, text="SECRET PAGE TEXT"))
    assert r.status is CallStatus.OK
    pm = json.loads(s.rows()[0]["response_json"])["provider_metrics"]
    for k in ("request_bytes", "prompt_tokens", "completion_tokens", "prompt_eval_ms", "eval_ms", "load_ms", "total_ms",
              "reasoning_chars", "context_used_pct", "num_ctx", "num_predict", "think"):
        assert k in pm, k
    assert pm["think"] is False and pm["num_ctx"] == 12288 and pm["completion_tokens"] > 0 and pm["eval_ms"] > 0
    assert "SECRET PAGE TEXT" not in json.dumps(pm)                      # metrics never carry content


# ------------------------------------------------------------------ context window
def test_prompt_larger_than_num_ctx_is_visible_in_the_metrics_instead_of_silent(tmp_path):
    fake = StrictOllama(intended(40))
    p = ollama(fake, num_ctx=1024)
    big = req(messages=(Message("user", "y" * 30_000),), reasoning="none")
    r = p.complete(big)
    assert r.metrics["prompt_tokens"] == 1024 and r.metrics["context_used_pct"] >= 100  # the server truncated; we can now see it


# ------------------------------------------------------------------ tools
def test_with_tools_the_grammar_is_omitted_so_a_tool_call_remains_possible():
    fake = StrictOllama(intended(10))
    tools = (ToolDescriptor("get_page_excerpt", "read", {"type": "object"}),)
    ollama(fake).complete(req(tools=tools))
    b = fake.calls[0]["body"]
    assert "format" not in b and b["tools"][0]["function"]["name"] == "get_page_excerpt" and '"answer"' in b["messages"][0]["content"]


def test_tool_calls_are_parsed_with_generated_ids_and_dict_arguments():
    class Tools:
        def post(self, url, *, headers, body, timeout):
            return HttpReply(200, {}, json.dumps({"model": "m", "done": True, "done_reason": "stop", "message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "get_page_excerpt", "arguments": {"page_id": 3}}}, {"function": {"name": "x", "arguments": "{bad"}}]}}))
    r = ollama(Tools()).complete(req())
    assert [c.name for c in r.tool_calls] == ["get_page_excerpt", "x"] and r.tool_calls[0].arguments == {"page_id": 3}
    assert r.tool_calls[1].arguments == {} and r.tool_calls[0].call_id == "call_0" and r.finish_reason == "tool_calls"


# ------------------------------------------------------------------ errors and retry policy
def test_error_classification_and_retry_flags():
    fake = StrictOllama(intended(10))
    fake.mode = "404"
    with pytest.raises(ProviderTransportError) as e:
        ollama(fake).complete(req())
    assert not e.value.retryable and "not found" in str(e.value)       # a missing model is a configuration error: say so, do not retry
    fake.mode = "500"
    with pytest.raises(ProviderTransportError) as e:
        ollama(fake).complete(req())
    assert e.value.retryable
    for mode, retryable in (("timeout", False), ("connection", True)):
        fake.mode = mode
        with pytest.raises(ProviderTransportError) as e:
            ollama(fake).complete(req())
        assert e.value.retryable is retryable, mode


def test_a_timeout_costs_exactly_one_request_through_the_runner(tmp_path):
    fake = StrictOllama(intended(10))
    fake.mode = "timeout"
    s = wire(tmp_path, fake)
    r = s.runner.invoke(ECHO, s.req(ECHO))
    assert r.error.kind is FailureKind.TRANSPORT and len(fake.calls) == 1 and s.clock.sleeps == []


def test_empty_content_is_retryable_unless_the_cap_was_the_reason():
    class Empty:
        def __init__(self, reason): self.reason = reason
        def post(self, url, *, headers, body, timeout):
            return HttpReply(200, {}, json.dumps({"model": "m", "done": True, "done_reason": self.reason, "message": {"role": "assistant", "content": "", "thinking": "t" * 4000}}))
    with pytest.raises(ProviderTransportError, match="no content") as e:
        ollama(Empty("stop")).complete(req())
    assert e.value.retryable
    r = ollama(Empty("length")).complete(req())          # all budget went to thinking: reported as truncation, not as an outage
    assert r.finish_reason == "length" and r.content is None and r.metrics["reasoning_chars"] == 4000


# ------------------------------------------------------------------ configuration
def test_factory_builds_the_native_provider_from_settings(tmp_path):
    base = dict(_env_file=None, db_path=tmp_path / "x.db", cache_dir=tmp_path / "c", log_dir=tmp_path / "l")
    fake = StrictOllama(intended(10))
    p = build_provider_from_settings(Settings(**base, llm_provider="ollama", llm_model="qwen3:4b"), transport=fake)
    assert isinstance(p, OllamaProvider) and not isinstance(p, UnconfiguredProvider)
    p.complete(req())
    assert fake.calls[0]["url"] == "http://localhost:11434/api/chat" and fake.calls[0]["body"]["options"]["num_ctx"] == 12288
    custom = StrictOllama(intended(10))
    st = Settings(**base, llm_provider="ollama", llm_model="m", llm_base_url="http://host.docker.internal:11434/", ollama_num_ctx=16384,
                  ollama_keep_alive="10m")
    build_provider_from_settings(st, transport=custom).complete(req())
    assert custom.calls[0]["url"] == "http://host.docker.internal:11434/api/chat"   # LLM_BASE_URL is honoured now
    assert custom.calls[0]["body"]["options"]["num_ctx"] == 16384 and custom.calls[0]["body"]["keep_alive"] == "10m"
    with pytest.raises(ValueError, match="transport"):
        build_provider_from_settings(Settings(**base, llm_provider="ollama", llm_model="m"))


def test_extraction_reasoning_setting_reaches_the_runner(tmp_path):
    import corpdisc.app as app
    fake = StrictOllama(intended(10))
    for setting, expected in (("none", False), ("default", None)):
        st = Settings(_env_file=None, start_url="https://root.test/", root_company="R", root_code="R", db_path=tmp_path / f"{setting}.db",
                      cache_dir=tmp_path / f".c{setting}", log_dir=tmp_path / "l", use_playwright="false", llm_provider="ollama",
                      llm_model="qwen3:4b", llm_extraction_reasoning=setting)
        rt = app.build_runtime(st, transport=fake, http=type("H", (), {"get": lambda *a, **k: None, "close": lambda s: None})())
        assert rt.deps.runner._reasoning.get(AgentName.EXTRACTION) == ("none" if setting == "none" else None)
        rt.close()


def test_compact_schema_drops_noise_but_keeps_structure():
    c = compact_schema(SCHEMA)
    assert "title" not in json.dumps(c) and c["required"] == SCHEMA["required"] and c["properties"]["answer"]["type"] == "string"
    assert len(json.dumps(c)) < len(json.dumps(SCHEMA))
