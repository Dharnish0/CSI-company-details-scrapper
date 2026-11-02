"""SlowCrawlLimiter: single flight, 90 s start-to-start gap, adaptive pacing, 429 cooldown, TPD bucket (FakeClock)."""
from __future__ import annotations

import random
import threading
import time

import pytest

from corpdisc.agents.ratelimit import (
    Acquired,
    LimiterConfig,
    Refused,
    SlowCrawlLimiter,
    TokenBucket,
    classify_429,
)
from tests.helpers.clock import FakeClock


def make(clock=None, *, stop_check=None, deadline=None, **cfg):
    clock = clock or FakeClock()
    base = dict(interval_s=90.0, tpm_limit=8000, tpd_limit=0, tpm_target_fraction=0.70, daily_retry_s=900.0,
                sleep_chunk_s=30.0)
    base.update(cfg)
    lim = SlowCrawlLimiter(LimiterConfig(**base), clock, clock, rng=random.Random(1), stop_check=stop_check,
                           deadline=deadline)
    return lim, clock


def go(lim, est_in=100, max_out=100):
    a = lim.acquire(est_in, max_out)
    assert isinstance(a, Acquired), a
    return a


def ok(lim, tin=500, tout=500, metrics=None):
    lim.release_success(metrics or {}, tin, tout)


# ------------------------------------------------------------------ interval
def test_first_request_is_immediate_and_second_waits_the_interval():
    lim, clock = make()
    a = go(lim)
    assert a.waited_s == 0 and clock.sleeps == []
    ok(lim)
    b = go(lim)
    assert b.started_at - a.started_at >= 90.0 - 1e-6
    assert sum(clock.sleeps) == pytest.approx(90.0)


def test_gap_is_start_to_start_not_end_to_start():
    lim, clock = make()
    a = go(lim)
    clock.advance(25)  # the request itself takes 25 s
    ok(lim)
    b = go(lim)
    assert b.started_at - a.started_at == pytest.approx(90.0)


def test_sleeps_are_chunked_to_the_configured_chunk():
    lim, clock = make(sleep_chunk_s=30.0)
    go(lim)
    ok(lim)
    go(lim)
    assert clock.sleeps and max(clock.sleeps) <= 30.0 and sum(clock.sleeps) == pytest.approx(90.0)


def test_zero_interval_never_waits():
    lim, clock = make(interval_s=0.0, tpm_limit=0)
    for _ in range(3):
        go(lim)
        ok(lim, 10, 10)
    assert clock.sleeps == []


# ------------------------------------------------------------------ adaptive pacing
def test_gap_widens_to_cover_token_reset_when_remaining_is_low():
    lim, clock = make()
    a = go(lim)
    ok(lim, metrics={"rl_limit_tokens": 8000, "rl_remaining_tokens": 200, "rl_reset_tokens_s": 130.0})
    b = go(lim)
    assert b.started_at - a.started_at >= 130.0 - 1e-6


def test_low_request_bucket_widens_to_request_reset():
    lim, clock = make()
    a = go(lim)
    ok(lim, metrics={"rl_remaining_requests": 3, "rl_reset_requests_s": 200.0})
    assert go(lim).started_at - a.started_at >= 200.0 - 1e-6


def test_comfortable_remaining_tokens_do_not_widen_the_gap():
    lim, clock = make()
    a = go(lim)
    ok(lim, metrics={"rl_limit_tokens": 8000, "rl_remaining_tokens": 7000, "rl_reset_tokens_s": 130.0})
    assert go(lim).started_at - a.started_at == pytest.approx(90.0)


def test_gap_widens_when_projected_tpm_exceeds_target_fraction():
    lim, clock = make(interval_s=10.0)
    a = go(lim)
    ok(lim, 5000, 1000, metrics={"rl_limit_tokens": 8000})  # 6000 tokens: 6000 / (0.7 * 8000) * 60 = 64.29 s
    b = go(lim)
    assert b.started_at - a.started_at == pytest.approx(6000 / 5600 * 60, abs=0.01)
    assert lim.snapshot()["limit_tokens"] == 8000


def test_configured_tpm_limit_is_used_when_headers_are_absent():
    lim, clock = make(interval_s=10.0, tpm_limit=4000)
    a = go(lim)
    ok(lim, 1500, 500)  # 2000 / (0.7 * 4000) * 60 = 42.86 s
    assert go(lim).started_at - a.started_at == pytest.approx(2000 / 2800 * 60, abs=0.01)


# ------------------------------------------------------------------ 429 handling
def test_each_429_doubles_the_multiplier_up_to_eight():
    lim, clock = make(interval_s=10.0)
    seen = []
    for _ in range(5):
        go(lim)
        lim.release_429("minute", 1.0)
        seen.append(lim.snapshot()["multiplier"])
    assert seen == [2.0, 4.0, 8.0, 8.0, 8.0]
    assert lim.snapshot()["gap_s"] == 80.0 and lim.rate_limited == 5


def test_multiplier_shrinks_085_per_three_successes_and_never_below_one():
    lim, clock = make(interval_s=10.0)
    go(lim)
    lim.release_429("minute", 1.0)
    go(lim)
    lim.release_429("minute", 1.0)  # multiplier 4
    mults = []
    for _ in range(40):
        go(lim)
        ok(lim, 10, 10)
        mults.append(lim.snapshot()["multiplier"])
    assert mults[0] == mults[1] == 4.0 and mults[2] == pytest.approx(3.4)
    assert mults[5] == pytest.approx(3.4 * 0.85)
    assert min(mults) == 1.0 and mults[-1] == 1.0
    assert lim.snapshot()["gap_s"] >= 10.0


def test_a_429_resets_the_success_streak():
    lim, clock = make(interval_s=10.0)
    for _ in range(2):
        go(lim)
        ok(lim, 10, 10)
    go(lim)
    lim.release_429("minute", 1.0)
    go(lim)
    ok(lim, 10, 10)
    assert lim.snapshot()["multiplier"] == 2.0  # the earlier two successes no longer count towards a shrink


def test_retry_after_575_is_waited_in_full_with_small_positive_jitter():
    lim, clock = make(interval_s=10.0)
    go(lim)
    t0 = clock.monotonic()
    lim.release_429("day", 575.0)
    b = go(lim)
    waited = b.started_at - t0
    assert 575.0 <= waited < 577.0
    assert lim.cooldown_remaining() == 0


def test_cooldown_remaining_reports_the_pending_wait():
    lim, clock = make(interval_s=10.0)
    go(lim)
    lim.release_429("minute", 30.0)
    assert 30.0 <= lim.cooldown_remaining() < 32.0


def test_429_without_retry_after_uses_scope_defaults():
    lim, clock = make(interval_s=10.0, daily_retry_s=900.0)
    go(lim)
    day = lim.release_429("day", None)
    assert 900.0 <= day < 902.0
    go(lim)
    minute = lim.release_429("minute", None)
    assert 60.0 <= minute < 62.0


@pytest.mark.parametrize("detail,retry_after,expected", [
    ("Rate limit reached ... on tokens per day (TPD): Limit 200000", 30, "day"),
    ("requests per day (RPD)", None, "day"),
    ("(TPD) exceeded", None, "day"),
    ("Rate limit reached on tokens per minute (TPM): Limit 8000", 3000, "minute"),
    ("requests per minute (RPM)", None, "minute"),
    ("(TPM)", 900, "minute"),
    ("too many requests", None, "minute"),
    ("too many requests", 30, "minute"),
    ("too many requests", 65, "minute"),
    ("too many requests", 65.1, "day"),
    ("", 575, "day"),
    ("", None, "minute"),
])
def test_classify_429(detail, retry_after, expected):
    assert classify_429(detail, retry_after) == expected


def test_release_error_keeps_interval_without_widening():
    lim, clock = make(interval_s=10.0)
    a = go(lim)
    lim.release_error()
    assert lim.snapshot()["multiplier"] == 1.0
    assert go(lim).started_at - a.started_at == pytest.approx(10.0)


# ------------------------------------------------------------------ token bucket
def test_bucket_refills_continuously_not_in_a_sliding_window():
    b = TokenBucket(200_000, now=0.0)
    b.consume(200_000, 0.0)
    assert b.level_at(0.0) == 0
    assert b.level_at(575.0) == pytest.approx(575 * 200_000 / 86400)  # about 1,331 tokens, as the quota probe showed
    c = TokenBucket(200_000, now=0.0)
    c.consume(200_000, 0.0)
    assert c.wait_for(1330, 0.0) == pytest.approx(1330 * 86400 / 200_000)


def test_bucket_replay_from_past_usage():
    b = TokenBucket(100_000, now=0.0)
    b.replay([(3600.0, 60_000), (60.0, 30_000)], now=10_000.0)
    expected = 100_000 - 60_000 + 3540 * 100_000 / 86400 - 30_000 + 60 * 100_000 / 86400
    assert b.level_at(10_000.0) == pytest.approx(expected, abs=1.0)
    empty = TokenBucket(1000, now=0.0)
    empty.replay([], now=5.0)
    assert empty.level_at(5.0) == 1000


def test_bucket_replay_cannot_go_negative_and_disabled_bucket_is_inert():
    b = TokenBucket(1000, now=0.0)
    b.replay([(10.0, 5000)], now=100.0)
    assert b.level_at(100.0) >= 0
    off = TokenBucket(0)
    off.consume(10**9, 1.0)
    assert off.wait_for(10**9, 1.0) == 0.0


def test_limiter_waits_proactively_when_the_daily_estimate_is_low():
    clock = FakeClock()
    bucket = TokenBucket(200_000, now=clock.monotonic())
    bucket.consume(199_000, clock.monotonic())  # 1,000 left
    lim = SlowCrawlLimiter(LimiterConfig(interval_s=0, tpm_limit=0, tpd_limit=200_000), clock, clock,
                           rng=random.Random(1), bucket=bucket)
    t0 = clock.monotonic()
    a = lim.acquire(2000, 1000)  # projected 2000 + 1000 default completion
    assert a.started_at - t0 == pytest.approx((3000 - 1000) * 86400 / 200_000, rel=1e-3)


def test_day_429_lowers_the_estimate_so_the_next_request_fits_at_wake_time():
    clock = FakeClock()
    lim = SlowCrawlLimiter(LimiterConfig(interval_s=0, tpm_limit=0, tpd_limit=200_000), clock, clock,
                           rng=random.Random(1))
    go(lim, 2000, 1000)
    t0 = clock.monotonic()
    lim.release_429("day", 575.0)
    b = go(lim, 2000, 1000)
    assert 575.0 <= b.started_at - t0 < 580.0  # no extra proactive wait beyond the stated Retry-After


# ------------------------------------------------------------------ stop and deadline
def test_stop_check_during_a_wait_refuses_and_frees_the_slot():
    state = {"stop": None}
    lim, clock = make(stop_check=lambda: state["stop"])
    go(lim)
    ok(lim)

    def on_sleep_stop():
        state["stop"] = "SHUTDOWN"

    orig = clock.sleep

    def sleep(sec):
        orig(sec)
        on_sleep_stop()

    lim._sleeper = type("S", (), {"sleep": staticmethod(sleep)})()
    res = lim.acquire(100, 100)
    assert res == Refused("SHUTDOWN")
    state["stop"] = None
    lim._sleeper = clock
    assert isinstance(lim.acquire(100, 100), Acquired)  # the slot was released by the refusal


def test_stop_check_before_start_refuses_without_sleeping():
    lim, clock = make(stop_check=lambda: "SHUTDOWN")
    assert lim.acquire(1, 1) == Refused("SHUTDOWN") and clock.sleeps == []


def test_wait_beyond_the_deadline_is_refused_up_front():
    clock = FakeClock()
    lim, _ = make(clock, deadline=clock.monotonic() + 60.0)
    go(lim)
    ok(lim)
    assert lim.acquire(1, 1) == Refused("MAX_RUNTIME")  # next start in 90 s, deadline in 60 s
    assert clock.sleeps == []


def test_deadline_already_passed_is_refused():
    clock = FakeClock()
    lim, _ = make(clock, deadline=clock.monotonic() - 1.0)
    assert lim.acquire(1, 1) == Refused("MAX_RUNTIME")


def test_request_that_fits_before_the_deadline_is_allowed():
    clock = FakeClock()
    lim, _ = make(clock, deadline=clock.monotonic() + 1000.0)
    go(lim)
    ok(lim)
    assert isinstance(lim.acquire(1, 1), Acquired)


# ------------------------------------------------------------------ concurrency
def test_two_threads_never_hold_a_slot_at_once():
    lim, clock = make(interval_s=0.0)
    inside, peak, lock, errors = [0], [0], threading.Lock(), []

    def worker():
        try:
            for _ in range(10):
                a = lim.acquire(10, 10)
                assert isinstance(a, Acquired)
                with lock:
                    inside[0] += 1
                    peak[0] = max(peak[0], inside[0])
                time.sleep(0.001)
                with lock:
                    inside[0] -= 1
                lim.release_success({}, 5, 5)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors and peak[0] == 1 and lim.requests == 20
