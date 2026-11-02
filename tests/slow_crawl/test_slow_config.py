"""Slow-crawl settings: defaults, env overrides, blank ceilings and range checks."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from corpdisc.config import Settings


def make(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_defaults_leave_slow_crawl_off():
    s = make()
    assert s.slow_crawl is False
    assert s.slow_crawl_interval_seconds == 90
    assert s.slow_crawl_max_runtime_hours == 24
    assert s.slow_crawl_token_ceiling == 250_000
    assert s.slow_crawl_cost_ceiling_usd is None
    assert s.slow_crawl_tpm_limit == 8000
    assert s.slow_crawl_tpd_limit == 200_000
    assert s.slow_crawl_tpm_target_fraction == 0.70
    assert s.slow_crawl_daily_retry_seconds == 900
    assert s.slow_crawl_sleep_chunk_seconds == 30
    assert s.slow_crawl_progress_interval_seconds == 300


def test_existing_rate_limit_defaults_are_untouched():
    s = make()
    assert (s.llm_rpm_limit, s.llm_tpm_limit, s.llm_rate_limit_retries) == (-1, -1, 5)
    assert s.llm_retry_after_ceiling_seconds == 120 and s.queue_retry_wait_max_seconds == 300


def test_env_enables_slow_crawl(monkeypatch):
    monkeypatch.setenv("SLOW_CRAWL", "true")
    monkeypatch.setenv("SLOW_CRAWL_INTERVAL_SECONDS", "120")
    monkeypatch.setenv("SLOW_CRAWL_COST_CEILING_USD", "1.5")
    s = make()
    assert s.slow_crawl is True and s.slow_crawl_interval_seconds == 120 and s.slow_crawl_cost_ceiling_usd == 1.5


def test_blank_cost_ceiling_is_none(monkeypatch):
    monkeypatch.setenv("SLOW_CRAWL_COST_CEILING_USD", "  ")
    assert make().slow_crawl_cost_ceiling_usd is None


@pytest.mark.parametrize("kw", [
    {"slow_crawl_interval_seconds": -1},
    {"slow_crawl_max_runtime_hours": 0},
    {"slow_crawl_max_runtime_hours": 169},
    {"slow_crawl_token_ceiling": 0},
    {"slow_crawl_cost_ceiling_usd": -0.01},
    {"slow_crawl_tpm_limit": -1},
    {"slow_crawl_tpd_limit": -1},
    {"slow_crawl_tpm_target_fraction": 0},
    {"slow_crawl_tpm_target_fraction": 1.01},
    {"slow_crawl_daily_retry_seconds": 59},
    {"slow_crawl_sleep_chunk_seconds": 0.5},
    {"slow_crawl_sleep_chunk_seconds": 61},
    {"slow_crawl_progress_interval_seconds": 9},
])
def test_out_of_range_values_are_rejected(kw):
    with pytest.raises(ValidationError):
        make(**kw)


def test_boundary_values_are_accepted():
    s = make(slow_crawl_interval_seconds=0, slow_crawl_max_runtime_hours=168, slow_crawl_tpd_limit=0,
             slow_crawl_tpm_target_fraction=1, slow_crawl_sleep_chunk_seconds=60, slow_crawl_cost_ceiling_usd=0)
    assert s.slow_crawl_tpd_limit == 0 and s.slow_crawl_cost_ceiling_usd == 0
