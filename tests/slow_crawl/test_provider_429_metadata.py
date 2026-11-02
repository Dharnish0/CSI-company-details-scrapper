"""ProviderError carries redacted 429 detail and whitelisted rate-limit metrics without changing str(exc)."""
from __future__ import annotations

import json

import pytest

from corpdisc.agents.ports import ProviderAuthError, ProviderError, ProviderTransportError
from corpdisc.agents.providers.http_common import classify, retry_after
from corpdisc.transport import HttpReply

KEY = "gsk_SECRETKEY1234567890"


def reply(status, message="", headers=None):
    body = json.dumps({"error": {"message": message}}) if message else "{}"
    return HttpReply(status=status, headers={k.lower(): v for k, v in (headers or {}).items()}, body=body)


def test_default_constructor_is_unchanged():
    e = ProviderError("x")
    assert e.detail == "" and e.rl_metrics is None and not e.rate_limited and str(e) == "x"


def test_429_message_text_is_unchanged_and_detail_is_filled():
    msg = ("Rate limit reached for model m in organization org_x service tier on_demand on tokens per day (TPD): "
           "Limit 200000, Used 199000, Requested 2330. Please try again in 9m35s.")
    err = classify(reply(429, msg, {"retry-after": "575"}), provider="groq", secret=KEY)
    assert isinstance(err, ProviderTransportError) and err.rate_limited and err.retryable
    assert str(err) == "groq transient error 429"
    assert "tokens per day (TPD)" in err.detail and len(err.detail) <= 200
    assert err.retry_after_s == 575


def test_detail_never_contains_the_key_or_a_bearer_token():
    msg = f"bad request using {KEY} and header Bearer abc.def.ghi, limit per minute"
    err = classify(reply(429, msg), provider="groq", secret=KEY)
    assert KEY not in err.detail and "Bearer" not in err.detail and "abc.def.ghi" not in err.detail
    assert "[REDACTED]" in err.detail
    assert KEY not in str(err)


def test_rl_metrics_contains_only_whitelisted_rl_keys():
    headers = {"x-ratelimit-limit-tokens": "8000", "x-ratelimit-remaining-tokens": "120",
               "x-ratelimit-reset-tokens": "7.66s", "x-ratelimit-limit-requests": "1000",
               "x-ratelimit-remaining-requests": "990", "x-ratelimit-reset-requests": "2m59.56s",
               "authorization": f"Bearer {KEY}", "set-cookie": "a=b", "x-request-id": "req_1"}
    err = classify(reply(429, "per minute", headers), provider="groq", secret=KEY)
    assert err.rl_metrics is not None and set(err.rl_metrics) <= {
        "rl_limit_requests", "rl_limit_tokens", "rl_remaining_requests", "rl_remaining_tokens",
        "rl_reset_requests_s", "rl_reset_tokens_s"}
    assert err.rl_metrics["rl_limit_tokens"] == 8000 and err.rl_metrics["rl_remaining_tokens"] == 120
    assert err.rl_metrics["rl_reset_tokens_s"] == pytest.approx(7.66)
    assert err.rl_metrics["rl_reset_requests_s"] == pytest.approx(179.56)
    assert KEY not in repr(err.rl_metrics)


def test_no_headers_gives_empty_metrics_dict():
    err = classify(reply(429, "x"), provider="groq", secret=None)
    assert err.rl_metrics == {}


@pytest.mark.parametrize("status", [408, 500, 503, 529])
def test_other_transient_statuses_also_carry_metadata(status):
    err = classify(reply(status, "upstream", {"x-ratelimit-remaining-tokens": "5"}), provider="groq", secret=KEY)
    assert err.retryable and not err.rate_limited and err.detail == "upstream"
    assert err.rl_metrics == {"rl_remaining_tokens": 5}
    assert str(err) == f"groq transient error {status}"


def test_non_transient_errors_are_unchanged():
    auth = classify(reply(401, "bad key"), provider="groq", secret=KEY)
    assert isinstance(auth, ProviderAuthError) and str(auth) == "groq auth/quota error 401: bad key"
    rej = classify(reply(400, "nope"), provider="groq", secret=KEY)
    assert not rej.retryable and str(rej) == "groq rejected the request (400): nope"
    assert classify(reply(200), provider="groq", secret=KEY) is None


def test_retry_after_duration_string_is_parsed():
    assert retry_after({"retry-after": "9m35s"}) == 575
    assert retry_after({"retry-after": "30"}) == 30
    assert retry_after({}) is None


@pytest.mark.parametrize("module", ["groq", "openai", "openai_compat", "ollama", "anthropic"])
def test_http_providers_route_errors_through_classify(module):
    from pathlib import Path

    import corpdisc.agents.providers as pkg
    src = (Path(pkg.__file__).parent / f"{module}.py").read_text(encoding="utf-8")
    assert "classify(reply" in src
