from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from corpdisc.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def make(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_m1_defaults():
    s = make()
    assert s.max_response_bytes == 2_000_000
    assert s.fetch_max_redirects == 5
    assert s.page_cache_ttl_seconds > 0
    assert s.playwright_timeout_seconds > 0 and s.playwright_min_text_chars >= 0
    assert s.robots_failure_cooldown_seconds >= 0
    assert s.require_website_for_confirmed is True
    assert s.allow_private_hosts is False
    assert s.llm_token_budget is None
    assert "utm_*" in s.tracking_query_params and "fbclid" in s.tracking_query_params
    assert "linkedin.com" in s.shared_platform_domains


def test_list_settings_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SHARED_PLATFORM_DOMAINS", "Foo.com, bar.org ,,foo.com")
    monkeypatch.setenv("TRACKING_QUERY_PARAMS", "ref,track_*")
    s = make()
    assert s.shared_platform_domains == ["foo.com", "bar.org"]
    assert s.tracking_query_params == ["ref", "track_*"]


def test_env_overrides_and_blank_budget(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("REQUIRE_WEBSITE_FOR_CONFIRMED", "false")
    monkeypatch.setenv("LLM_TOKEN_BUDGET", "")
    monkeypatch.setenv("MAX_RESPONSE_BYTES", "4096")
    s = make()
    assert s.require_website_for_confirmed is False
    assert s.llm_token_budget is None
    assert s.max_response_bytes == 4096
    monkeypatch.setenv("LLM_TOKEN_BUDGET", "5000")
    assert make().llm_token_budget == 5000


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_response_bytes": 10},
        {"fetch_max_redirects": -1},
        {"fetch_max_redirects": 99},
        {"page_cache_ttl_seconds": -1},
        {"playwright_timeout_seconds": 0},
        {"playwright_min_text_chars": -1},
        {"robots_failure_cooldown_seconds": -5},
        {"llm_token_budget": 0},
        {"max_llm_calls_per_task": 0},
        {"max_tool_calls_per_run": 0},
    ],
)
def test_m1_setting_ranges_are_validated(kwargs):
    with pytest.raises(ValidationError):
        make(**kwargs)


def test_env_example_lists_every_setting():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    names = {line.split("=", 1)[0].strip() for line in text.splitlines()
             if "=" in line and not line.lstrip().startswith("#")}
    missing = [f.upper() for f in Settings.model_fields if f.upper() not in names]
    assert not missing, f".env.example missing {missing}"
