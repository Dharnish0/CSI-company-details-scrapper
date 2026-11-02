"""End-to-end slow crawl scenarios: the real ``GroqProvider`` and gateway talk to a scripted fake transport (``FakeGroq``),
the fixture world supplies the hierarchy, and a fake clock makes hours of waiting instant. No network, no sleeping."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta
from types import SimpleNamespace

import pytest

from corpdisc.graphs.slow_crawl import INTERRUPTED, MAX_RUNTIME
from corpdisc.services.budget import SessionCeiling
from corpdisc.services.instance_lock import InstanceLock, LockHeld
from corpdisc.services.progress import build_progress
from corpdisc.services.shutdown import ShutdownFlag
from tests.fixtures.world import seeded_world
from tests.helpers.fake_groq import FakeGroq, day_429, minute_429, on_request
from tests.slow_crawl.test_supervisor_slow import SLOW, attach, set_deadline, snapshot

SCENARIO = {**SLOW, "slow_crawl_interval_seconds": 90}  # the production starting interval


def groq_world(tmp_path, *, faults=(), slow=True, settings_kw=None, on_post=None, provider_kw=None, **attach_kw):
    """Fixture world whose gateway uses the real GroqProvider on a FakeGroq transport."""
    w = seeded_world(tmp_path, settings_kw={**SCENARIO, **(settings_kw or {})})
    fake = FakeGroq(w.brain, w.clock, faults=list(faults), on_post=on_post)
    w.deps.runner.gateway._provider = fake.provider(**(provider_kw or {}))
    ctl = attach(w, **attach_kw) if slow else None
    return w, fake, ctl


def done_rows(w):
    return [tuple(r) for r in w.q("SELECT * FROM research_queue WHERE status='DONE' ORDER BY task_id")]


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    """An uninterrupted default-mode crawl through the same provider stack."""
    w, fake, _ = groq_world(tmp_path_factory.mktemp("default"), slow=False)
    assert w.sup.run().status == "COMPLETED"
    return SimpleNamespace(w=w, fake=fake, snap=snapshot(w), posts=len(fake.starts), tokens=sum(fake.tokens),
                           tasks=len(w.q("SELECT 1 FROM research_queue")))


# --------------------------------------------------------------------------------------------- default mode
def test_default_mode_has_no_limiter_and_never_sleeps_for_pacing(baseline):
    w, fake = baseline.w, baseline.fake
    assert w.deps.runner.gateway.limiter is None and w.sup.slow is None
    assert w.clock.sleeps == [] and fake.peak_in_flight == 1
    assert {"ExtractionOutput", "RelationshipOutput", "WebsiteOutput"} <= set(fake.schemas)
    assert baseline.posts >= 8


# --------------------------------------------------------------------------------------------- single flight and pacing
def test_one_interval_applies_across_extraction_relationship_and_website_requests(tmp_path, baseline):
    w, fake, ctl = groq_world(tmp_path)
    t0 = w.clock.monotonic()
    s = w.sup.run()
    assert s.status == "COMPLETED" and snapshot(w) == baseline.snap
    assert {"ExtractionOutput", "RelationshipOutput", "WebsiteOutput"} <= set(fake.schemas)  # three different agents
    assert len(fake.starts) == baseline.posts and fake.starts[0] == t0  # the first request is immediate
    assert min(fake.gaps()) >= 90 - 1e-6  # start to start, whichever agent sent it
    assert fake.peak_in_flight == 1 and ctl.limiter.requests == len(fake.starts)
    assert max(w.clock.sleeps) <= 30
    assert w.q("SELECT status FROM runs")[0][0] == "COMPLETED"


def test_a_429_widens_the_gap_and_it_recovers_slowly_but_never_below_the_interval(tmp_path, baseline):
    w, fake, ctl = groq_world(tmp_path, faults=[on_request(3, reply=minute_429(30))])
    assert w.sup.run().status == "COMPLETED"
    gaps = fake.gaps()
    assert len(fake.starts) == baseline.posts + 1  # the refused request is retried inside the call
    assert gaps[1] == pytest.approx(90) and gaps[2] >= 180 - 1e-6  # after the 429 the gap doubles
    assert min(gaps) >= 90 - 1e-6
    assert min(gaps[3:]) < max(gaps[3:]) or gaps[3] == pytest.approx(90)  # not stuck at the widened gap
    assert gaps[-1] < 180 and ctl.limiter.snapshot()["multiplier"] < 2.0
    assert snapshot(w) == baseline.snap


def test_retry_after_is_honoured_before_the_next_provider_request(tmp_path, baseline):
    w, fake, ctl = groq_world(tmp_path, faults=[on_request(2, reply=minute_429(575))])  # root relationship; above the 120 s in-call ceiling
    s = w.sup.run()
    assert s.status == "COMPLETED" and s.retries == 1 and snapshot(w) == baseline.snap
    wait = fake.starts[2] - fake.starts[1]
    assert 575 <= wait < 580  # Retry-After plus under 2 s of jitter, never less
    assert max(w.clock.sleeps) <= 30
    assert len(fake.starts) == baseline.posts + 1  # every other request came from the cache or ran once


def test_cache_hits_do_not_consume_the_interval(tmp_path, baseline):
    w, fake, ctl = groq_world(tmp_path, faults=[on_request(2, reply=minute_429(300))])
    s = w.sup.run()
    assert s.status == "COMPLETED"
    assert fake.schemas.count("ExtractionOutput") == baseline.fake.schemas.count("ExtractionOutput")  # no chunk paid twice
    assert ctl.limiter.requests == len(fake.starts) == baseline.posts + 1  # only provider-reaching requests were counted
    after_cache = [b - a for a, b in zip(fake.starts, fake.starts[1:])]
    assert all(g >= 90 - 1e-6 for g in after_cache)


# --------------------------------------------------------------------------------------------- daily quota
def test_daily_quota_cools_down_every_pending_task_and_progress_reports_it(tmp_path, baseline):
    w, fake, ctl = groq_world(tmp_path, faults=[on_request(3, reply=day_429("9m35s"))])
    set_deadline(ctl, w.clock.monotonic() + 400)  # stop at the cool-down instead of sleeping through it
    t429 = None

    def mark(n, schema):
        nonlocal t429
        if n == 3:
            t429 = w.clock.now()
    fake.on_post = mark
    s = w.sup.run()
    assert (s.status, s.stopped_reason) == ("PAUSED", MAX_RUNTIME)
    pending = w.q("SELECT task_id, attempts, not_before FROM research_queue WHERE status='PENDING' ORDER BY task_id")
    assert len(pending) >= 2 and len({r["not_before"] for r in pending}) == 1  # every pending task has the same wake time
    wake = pending[0]["not_before"]
    assert wake >= (t429 + timedelta(seconds=575)).isoformat(timespec="microseconds")
    assert [r["attempts"] for r in pending] == [0] * len(pending)
    done_before = done_rows(w)
    assert done_before and w.q("SELECT COUNT(*) FROM research_queue WHERE status='RUNNING'")[0][0] == 0

    p = build_progress(w.conn, w.run_id, w.deps.settings, now=w.clock.now())  # the numbers a user would see
    assert p["run"]["status"] == "PAUSED" and p["api"]["http_429"] == 1
    assert p["tasks"]["waiting_retry"] == len(pending) and p["tasks"]["pending"] == 0 and p["tasks"]["running"] == 0
    assert p["next_retry"]["at"] == wake and p["next_retry"]["in_seconds"] >= 570
    assert p["usage"]["tokens"] == sum(fake.tokens) and p["api"]["successful_calls"] == len(fake.starts) - 1
    assert p["entities"]["confirmed"] == len(w.names("CONFIRMED"))

    # the next session reads the cool-down from the database and finishes the crawl
    started, first_new = len(fake.starts), []
    fake.faults.clear()
    fake.on_post = lambda n, schema: first_new.append(w.clock.now()) if n == started + 1 else None
    attach(w)
    final = w.sup.resume()
    assert final.status == "COMPLETED" and snapshot(w) == baseline.snap
    assert first_new[0].isoformat(timespec="microseconds") >= wake  # nothing was sent before the cool-down ended
    assert done_rows(w)[: len(done_before)] == done_before  # earlier finished tasks were not reset
    assert len(w.q("SELECT 1 FROM research_queue")) == baseline.tasks


def test_a_quota_that_never_recovers_stops_at_24_hours_with_everything_saved(tmp_path):
    out: list[str] = []
    w, fake, ctl = groq_world(tmp_path, faults=[lambda n, schema: day_429(3000) if n >= 3 else None], out=out,
                              settings_kw={"slow_crawl_progress_interval_seconds": 3600})
    t0 = w.clock.monotonic()
    s = w.sup.run()
    assert (s.status, s.stopped_reason) == ("PAUSED", MAX_RUNTIME)
    assert 24 * 3600 - 3100 <= w.clock.monotonic() - t0 <= 24 * 3600
    assert fake.statuses.count(429) <= 30 and max(w.clock.sleeps) <= 30  # one probe per cool-down, chunked sleeping
    assert w.q("SELECT COUNT(*) FROM research_queue WHERE status='RUNNING'")[0][0] == 0
    assert w.q("SELECT status FROM runs")[0][0] == "PAUSED"
    p = build_progress(w.conn, w.run_id, w.deps.settings, now=w.clock.now())
    assert p["api"]["http_429"] == fake.statuses.count(429) and p["tasks"]["done"] >= 1
    assert out and "Run 1" in out[-1]


# --------------------------------------------------------------------------------------------- spending guards
def test_token_ceiling_stops_before_the_next_request_and_a_new_session_finishes(tmp_path, baseline):
    w, fake, ctl = groq_world(tmp_path, ceiling=SessionCeiling(baseline_tokens=0, token_ceiling=baseline.tokens // 2))
    s = w.sup.run()
    assert s.status == "BUDGET_EXHAUSTED" and "SLOW_CRAWL_CEILING" in s.stopped_reason
    assert 0 < len(fake.starts) < baseline.posts
    sent = len(fake.starts)
    assert w.sup.run().status == "BUDGET_EXHAUSTED" and len(fake.starts) == sent  # still no request while over the ceiling
    assert w.q("SELECT COUNT(*) FROM research_queue WHERE status IN ('SKIPPED','RUNNING','FAILED')")[0][0] == 0
    assert w.q("SELECT status FROM runs")[0][0] == "BUDGET_EXHAUSTED"
    done_before = done_rows(w)
    # a new session starts a fresh allowance measured from what the run already holds
    spent = w.q("SELECT llm_tokens_used FROM runs")[0][0]
    attach(w, ceiling=SessionCeiling(baseline_tokens=spent, token_ceiling=250000))
    final = w.sup.resume()
    assert final.status == "COMPLETED" and snapshot(w) == baseline.snap
    assert len(fake.starts) == baseline.posts  # the denied call was never sent, nothing was asked twice
    assert done_rows(w)[: len(done_before)] == done_before


def test_cost_ceiling_stops_the_crawl_when_prices_are_set(tmp_path, baseline):
    prices = {"llm_price_in_per_mtok": 100.0, "llm_price_out_per_mtok": 200.0}
    w, fake, ctl = groq_world(tmp_path, settings_kw=prices, provider_kw={"price_in_per_mtok": 100.0, "price_out_per_mtok": 200.0},
                              ceiling=SessionCeiling(baseline_tokens=0, token_ceiling=10**9, baseline_cost=0.0,
                                                     cost_ceiling_usd=1.0, price_in_per_mtok=100.0, price_out_per_mtok=200.0))
    s = w.sup.run()
    assert s.status == "BUDGET_EXHAUSTED" and "SLOW_CRAWL_COST_CEILING" in s.stopped_reason
    cost = w.q("SELECT llm_cost_usd FROM runs")[0][0]
    assert 0 < cost <= 1.0 and 0 < len(fake.starts) < baseline.posts
    sent = len(fake.starts)
    again = w.sup.run()  # still over budget: no request, the task stays queued\n    assert "SLOW_CRAWL_COST_CEILING" in again.stopped_reason and len(fake.starts) == sent


# --------------------------------------------------------------------------------------------- shutdown and resume
def test_graceful_shutdown_requeues_the_running_task_and_resume_continues(tmp_path, baseline):
    flag = ShutdownFlag()
    w, fake, ctl = groq_world(tmp_path, flag=flag, on_post=lambda n, schema: flag.request() if n == 3 else None)
    s = w.sup.run()
    assert (s.status, s.stopped_reason) == ("PAUSED", INTERRUPTED)
    assert len(fake.starts) == 3  # the in-flight request finished, nothing new started
    assert w.q("SELECT llm_tokens_used FROM runs")[0][0] == sum(fake.tokens)  # and its tokens were recorded
    assert w.q("SELECT COUNT(*) FROM research_queue WHERE status='RUNNING'")[0][0] == 0
    assert all(r["attempts"] == 0 for r in w.q("SELECT attempts FROM research_queue WHERE status='PENDING'"))
    done_before = done_rows(w)
    fake.on_post = None
    attach(w)
    final = w.sup.resume()
    assert final.status == "COMPLETED" and snapshot(w) == baseline.snap
    assert len(fake.starts) == baseline.posts  # the answered request came from the cache, no request was repeated
    assert done_rows(w)[: len(done_before)] == done_before
    assert len(w.q("SELECT 1 FROM research_queue")) == baseline.tasks


def test_resume_after_a_default_mode_pause_continues_in_one_process(tmp_path, baseline):
    w, fake, _ = groq_world(tmp_path, slow=False, faults=[on_request(2, reply=minute_429(600))],
                            settings_kw={"queue_retry_wait_max_seconds": 0})
    first = w.sup.run()  # the existing behaviour: exit with PAUSED / RETRY_WAITING
    assert (first.status, first.stopped_reason) == ("PAUSED", "RETRY_WAITING")
    posted = len(fake.starts)
    attach(w)
    final = w.sup.resume()  # slow mode: wait for the retry time, then finish without being started again
    assert final.status == "COMPLETED" and snapshot(w) == baseline.snap
    assert len(fake.starts) == baseline.posts + 1  # one refused request, everything else asked exactly once
    assert fake.schemas[posted:].count("ExtractionOutput") == baseline.fake.schemas.count("ExtractionOutput") - 1
    assert len(w.q("SELECT 1 FROM research_queue")) == baseline.tasks  # no duplicate tasks
    assert w.q("SELECT COUNT(*) FROM entities")[0][0] == len(baseline.w.q("SELECT 1 FROM entities"))


# --------------------------------------------------------------------------------------------- single instance
def test_live_lock_blocks_a_second_crawler_and_a_dead_one_is_reclaimed(tmp_path):
    db = tmp_path / "bench.db"
    first = InstanceLock(db)
    first.acquire(run_id=1)
    try:
        with pytest.raises(LockHeld) as exc:
            InstanceLock(db).acquire(run_id=1)
        assert exc.value.pid == first.pid and str(first.path) in str(exc.value)
    finally:
        first.release()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    first.path.write_text(json.dumps({"pid": dead.pid}), encoding="utf-8")
    second = InstanceLock(db)
    second.acquire(run_id=1)
    try:
        assert second.reclaimed_from == dead.pid
    finally:
        second.release()
    assert not first.path.exists()
