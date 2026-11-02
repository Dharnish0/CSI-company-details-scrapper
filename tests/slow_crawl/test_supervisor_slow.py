"""Supervisor in slow crawl mode: sleeps until the next eligible task instead of pausing, keeps 429s attempt-neutral, persists
the daily cool-down, stops on runtime / ceilings / shutdown with every task put back unchanged. Fake clock, fake provider."""
from __future__ import annotations

import random
from datetime import timedelta
from types import SimpleNamespace

import pytest

from corpdisc.agents.ports import ProviderTransportError
from corpdisc.agents.ratelimit import LimiterConfig, SlowCrawlLimiter
from corpdisc.graphs.slow_crawl import INTERRUPTED, MAX_RUNTIME, SlowCrawlController
from corpdisc.graphs.supervisor import Supervisor
from corpdisc.services.budget import SessionCeiling
from corpdisc.services.instance_lock import InstanceLock, read_session
from corpdisc.services.shutdown import ShutdownFlag
from tests.fixtures.world import seeded_world

SLOW = {"slow_crawl": True, "slow_crawl_interval_seconds": 10, "slow_crawl_tpd_limit": 0, "slow_crawl_tpm_limit": 0}
DAY = "Rate limit reached for model in organization on tokens per day (TPD): Limit 200000"
MINUTE = "Rate limit reached for model on tokens per minute (TPM): Limit 8000"


def slow_world(tmp_path, **kw):
    return seeded_world(tmp_path, settings_kw={**SLOW, **kw})


def attach(w, *, hours=24.0, ceiling=None, flag=None, lock=None, sleeper=None, out=None):
    """Turn the world's supervisor into a slow crawl one (what ``app.build_runtime`` does when SLOW_CRAWL is on)."""
    clock = w.clock
    flag = flag or ShutdownFlag()
    sleeper = sleeper or clock
    deadline = clock.monotonic() + hours * 3600
    limiter = SlowCrawlLimiter(LimiterConfig.from_settings(w.deps.settings), clock, sleeper, rng=random.Random(0),
                               stop_check=flag.stop_check, deadline=deadline)
    w.deps.runner.gateway.limiter = limiter
    w.deps.budget.session = ceiling
    w.deps.budget._exclude_rate_limited = True
    ctl = SlowCrawlController(w.deps, limiter=limiter, sleeper=sleeper, deadline=deadline, stop_check=flag.stop_check,
                              lock=lock, emit=(out if out is not None else []).append)
    w.sup.slow = ctl
    return ctl


def set_deadline(ctl, mono):
    ctl.deadline = ctl.limiter.deadline = mono


def raise_429(schema, *, times=None, retry_after=None, detail="", armed=lambda: True):
    state = {"n": 0}

    def m(name, out):
        if name == schema and (times is None or state["n"] < times) and armed():
            state["n"] += 1
            raise ProviderTransportError("429", retryable=True, rate_limited=True, retry_after_s=retry_after, detail=detail)
        return out
    return m, state


def snapshot(w):
    return {
        "entities": sorted(tuple(r) for r in w.q("SELECT name, verification_status, entity_type FROM entities")),
        "tasks": sorted(tuple(r) for r in w.q("SELECT e.name, q.status FROM research_queue q JOIN entities e USING(entity_id)")),
        "cands": sorted(tuple(r) for r in w.q("SELECT raw_name, decision FROM candidates")),
    }


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    w = seeded_world(tmp_path_factory.mktemp("base"))
    assert w.sup.run().status == "COMPLETED"
    return w, snapshot(w)


# --------------------------------------------------------------------------------------------- default mode is untouched
def test_supervisor_without_slow_controller_is_the_default_loop(tmp_path):
    w = seeded_world(tmp_path)
    assert w.sup.slow is None and w.deps.runner.gateway.limiter is None and w.deps.budget.session is None
    assert Supervisor(w.deps, w.sup.runs).slow is None


# --------------------------------------------------------------------------------------------- idle wait instead of pause
def test_far_retry_after_sleeps_in_chunks_and_continues_instead_of_pausing(tmp_path, baseline):
    w = slow_world(tmp_path)
    attach(w)
    m, st = raise_429("RelationshipOutput", times=1, retry_after=300, detail=MINUTE)  # above the 120 s in-call ceiling
    w.brain.mutators.append(m)
    s = w.sup.run()
    assert s.status == "COMPLETED" and s.stopped_reason == "QUEUE_EMPTY" and s.retries == 1
    assert st["n"] == 1
    assert max(w.clock.sleeps) <= 30 and sum(w.clock.sleeps) >= 300  # chunked, and the whole Retry-After was waited
    assert s.waited_seconds >= 270
    row = w.q("SELECT status, attempts, error FROM research_queue WHERE task_id=1")[0]
    assert row["status"] == "DONE" and row["attempts"] == 1 and row["error"] is None
    assert w.brain.calls.count("ExtractionOutput") == baseline[0].brain.calls.count("ExtractionOutput")  # retry: cached
    assert snapshot(w) == baseline[1]


def test_rate_limit_deferrals_are_not_capped_in_slow_mode(tmp_path):
    w = slow_world(tmp_path, queue_max_rate_limit_deferrals=2)  # the default loop would give up on attempts after 2
    attach(w)
    m, st = raise_429("RelationshipOutput", times=5, retry_after=200, detail=MINUTE)
    w.brain.mutators.append(m)
    s = w.sup.run()
    assert s.status == "COMPLETED" and s.retries == 5 and st["n"] == 5
    assert w.q("SELECT status, attempts FROM research_queue WHERE task_id=1")[0][:] == ("DONE", 1)


def test_non_429_transient_failures_keep_the_attempt_consuming_path(tmp_path):
    w = slow_world(tmp_path)
    attach(w)

    def boom(name, out):
        if name == "RelationshipOutput":
            raise ProviderTransportError("503 upstream", retryable=True)
        return out
    w.brain.mutators.append(boom)
    s = w.sup.run()
    assert s.status == "PARTIAL" and s.stopped_reason == "QUEUE_EMPTY" and s.retries == 2
    row = w.q("SELECT status, attempts, error FROM research_queue WHERE task_id=1")[0]
    assert row["status"] == "DONE" and row["attempts"] == 3 and row["error"].startswith("PARTIAL:")


def test_resume_after_a_default_pause_continues_automatically_without_new_provider_calls(tmp_path, baseline):
    w = seeded_world(tmp_path, settings_kw={**SLOW, "queue_retry_wait_max_seconds": 0})
    m, _ = raise_429("RelationshipOutput", retry_after=600)
    w.brain.mutators.append(m)
    first = w.sup.run()  # the default loop: PAUSED / RETRY_WAITING
    assert first.status == "PAUSED" and first.stopped_reason == "RETRY_WAITING"
    assert w.brain.calls.count("ExtractionOutput") == 1
    w.brain.mutators.clear()
    attach(w)
    final = w.sup.resume()  # slow loop: sleeps until the task is due, then finishes in the same call
    assert final.status == "COMPLETED"
    assert w.brain.calls.count("ExtractionOutput") == baseline[0].brain.calls.count("ExtractionOutput")  # root chunk: cached
    assert snapshot(w) == baseline[1]  # no duplicate tasks or entities
    assert w.q("SELECT COUNT(*) FROM research_queue")[0][0] == len(baseline[1]["tasks"])


# --------------------------------------------------------------------------------------------- daily quota
def test_daily_quota_defers_every_pending_task_and_survives_a_restart(tmp_path, baseline):
    w = slow_world(tmp_path)
    ctl = attach(w)
    assert w.sup.run(max_tasks=1).stopped_reason == "MAX_TASKS"  # the root is DONE, Group A and Group B are queued
    root_before = tuple(w.q("SELECT * FROM research_queue WHERE task_id=1")[0])
    pending = w.q("SELECT task_id FROM research_queue WHERE status='PENDING' ORDER BY task_id")
    assert len(pending) >= 2
    m, st = raise_429("ExtractionOutput", times=1, retry_after=3000, detail=DAY)
    w.brain.mutators.append(m)
    set_deadline(ctl, w.clock.monotonic() + 1000)  # the wake time (about 3000 s away) is beyond the runtime limit
    t0 = w.clock.now()
    s = w.sup.run()
    assert (s.status, s.stopped_reason) == ("PAUSED", MAX_RUNTIME) and st["n"] == 1
    rows = w.q("SELECT task_id, status, attempts, not_before FROM research_queue WHERE task_id > 1 ORDER BY task_id")
    wakes = {r["not_before"] for r in rows if r["status"] == "PENDING"}
    assert len(wakes) == 1 and all(r["status"] == "PENDING" for r in rows)  # every pending task got the same wake time
    wake = next(iter(wakes))
    assert wake >= (t0 + timedelta(seconds=3000)).isoformat(timespec="microseconds")
    assert all(r["attempts"] == 0 for r in rows)  # a 429 never consumes an attempt
    assert tuple(w.q("SELECT * FROM research_queue WHERE task_id=1")[0]) == root_before  # the DONE task was not touched
    assert w.q("SELECT status FROM runs")[0][0] == "PAUSED"
    # a new process: fresh limiter and controller, the cool-down is read back from research_queue.not_before
    w.brain.mutators.clear()
    w.clock.sleeps.clear()
    attach(w)
    final = w.sup.resume()
    assert final.status == "COMPLETED" and w.clock.now().isoformat(timespec="microseconds") >= wake
    assert max(w.clock.sleeps) <= 30 and sum(w.clock.sleeps) >= 2900
    assert snapshot(w) == baseline[1]
    assert w.q("SELECT COUNT(*) FROM research_queue")[0][0] == len(baseline[1]["tasks"])  # no duplicate tasks


def test_a_day_quota_that_never_recovers_ends_at_the_24_hour_limit(tmp_path):
    out: list[str] = []
    w = slow_world(tmp_path, slow_crawl_progress_interval_seconds=3600)
    ctl = attach(w, out=out)
    start = w.clock.monotonic()
    m, st = raise_429("ExtractionOutput", retry_after=3000, detail=DAY)
    w.brain.mutators.append(m)
    s = w.sup.run()
    assert (s.status, s.stopped_reason) == ("PAUSED", MAX_RUNTIME)
    assert w.clock.monotonic() - start <= 24 * 3600 and w.clock.monotonic() - start >= 24 * 3600 - 3100
    assert 20 <= st["n"] <= 30  # about one probe per cool-down, not a request storm
    assert max(w.clock.sleeps) <= 30
    row = w.q("SELECT status, attempts FROM research_queue WHERE task_id=1")[0]
    assert (row["status"], row["attempts"]) == ("PENDING", 0)
    assert w.q("SELECT status FROM runs")[0][0] == "PAUSED"
    assert out and "Run 1" in out[-1]  # the final progress print
    assert ctl.http_429_day == st["n"]


# --------------------------------------------------------------------------------------------- shutdown
def test_shutdown_during_a_task_puts_it_back_unchanged_and_resume_finishes(tmp_path, baseline):
    w = slow_world(tmp_path)
    flag = ShutdownFlag()
    attach(w, flag=flag)

    def stop_after_extraction(name, out):
        if name == "ExtractionOutput":
            flag.request()  # the signal arrives while the request is in flight
        return out
    w.brain.mutators.append(stop_after_extraction)
    s = w.sup.run()
    assert (s.status, s.stopped_reason, s.processed) == ("PAUSED", INTERRUPTED, 0)
    assert w.brain.calls == ["ExtractionOutput"]  # the in-flight request finished, no new one started
    row = w.q("SELECT status, attempts, not_before, error FROM research_queue WHERE task_id=1")[0]
    assert (row["status"], row["attempts"], row["not_before"], row["error"]) == ("PENDING", 0, None, None)
    assert w.names() == {"Northwind Holdings"} and w.q("SELECT status FROM runs")[0][0] == "PAUSED"
    w.brain.mutators.clear()
    attach(w)  # next session
    final = w.sup.resume()
    assert final.status == "COMPLETED"
    assert w.brain.calls.count("ExtractionOutput") == baseline[0].brain.calls.count("ExtractionOutput")  # from the cache
    assert snapshot(w) == baseline[1]


def test_second_signal_inside_a_task_requeues_it_and_leaves_done_tasks_alone(tmp_path):
    w = slow_world(tmp_path)
    attach(w)
    assert w.sup.run(max_tasks=1).stopped_reason == "MAX_TASKS"
    root_before = tuple(w.q("SELECT * FROM research_queue WHERE task_id=1")[0])

    def ctrl_c(name, out):
        if name == "RelationshipOutput":
            raise KeyboardInterrupt
        return out
    w.brain.mutators.append(ctrl_c)
    s = w.sup.run()
    assert (s.status, s.stopped_reason) == ("PAUSED", INTERRUPTED)
    assert tuple(w.q("SELECT * FROM research_queue WHERE task_id=1")[0]) == root_before
    assert w.q("SELECT COUNT(*) FROM research_queue WHERE status='RUNNING'")[0][0] == 0
    assert w.q("SELECT attempts FROM research_queue WHERE status='PENDING' ORDER BY task_id")[0][0] == 0
    assert not w.conn.in_transaction


def test_shutdown_is_noticed_while_sleeping(tmp_path):
    w = slow_world(tmp_path)
    flag = ShutdownFlag()
    ctl = attach(w, flag=flag)
    calls = {"n": 0}

    class Trip:
        def sleep(self, seconds):
            w.clock.sleep(seconds)
            calls["n"] += 1
            if calls["n"] == 3:
                flag.request()
    ctl._sleeper = Trip()
    m, _ = raise_429("RelationshipOutput", times=1, retry_after=3000, detail=MINUTE)
    w.brain.mutators.append(m)
    s = w.sup.run()
    assert (s.status, s.stopped_reason) == ("PAUSED", INTERRUPTED) and calls["n"] == 3
    assert w.q("SELECT status FROM research_queue WHERE task_id=1")[0][0] == "PENDING"


# --------------------------------------------------------------------------------------------- ceilings
def test_session_token_ceiling_at_loop_top_stops_before_any_provider_call(tmp_path):
    w = slow_world(tmp_path)
    attach(w, ceiling=SessionCeiling(baseline_tokens=0, token_ceiling=400))
    w.conn.execute("UPDATE runs SET llm_tokens_used = 500")
    w.conn.commit()
    s = w.sup.run()
    assert s.status == "BUDGET_EXHAUSTED" and s.stopped_reason == "TOKEN_BUDGET_SLOW_CRAWL_CEILING"
    assert w.brain.calls == [] and w.q("SELECT status FROM research_queue")[0][0] == "PENDING"


def test_session_ceiling_is_measured_from_the_baseline(tmp_path):
    w = slow_world(tmp_path)
    w.conn.execute("UPDATE runs SET llm_tokens_used = 210224")  # earlier sessions already spent this
    w.conn.commit()
    attach(w, ceiling=SessionCeiling(baseline_tokens=210224, token_ceiling=250000))
    assert w.sup.run().status == "COMPLETED"


def test_token_ceiling_hit_by_a_call_hands_the_task_back_not_skips_it(tmp_path):
    w = slow_world(tmp_path)
    attach(w, ceiling=SessionCeiling(baseline_tokens=0, token_ceiling=1))  # no call fits
    s = w.sup.run()
    assert s.status == "BUDGET_EXHAUSTED" and "SLOW_CRAWL" in s.stopped_reason and w.brain.calls == []
    row = w.q("SELECT status, attempts FROM research_queue WHERE task_id=1")[0]
    assert (row["status"], row["attempts"]) == ("PENDING", 0)  # resumable in the next session, not SKIPPED
    assert w.q("SELECT status FROM runs")[0][0] == "BUDGET_EXHAUSTED"
    assert w.q("SELECT crawl_state FROM entities ORDER BY entity_id LIMIT 1")[0][0] != "BUDGET_STOPPED"
    assert dict(s.outcomes) == {} and s.processed == 0


def test_cost_ceiling_stops_before_any_provider_call(tmp_path):
    w = slow_world(tmp_path)
    attach(w, ceiling=SessionCeiling(baseline_tokens=0, token_ceiling=10**9, baseline_cost=0.0, cost_ceiling_usd=0.01,
                                     price_in_per_mtok=1.0, price_out_per_mtok=2.0))
    w.conn.execute("UPDATE runs SET llm_cost_usd = 0.02")
    w.conn.commit()
    s = w.sup.run()
    assert s.status == "BUDGET_EXHAUSTED" and s.stopped_reason == "TOKEN_BUDGET_SLOW_CRAWL_COST_CEILING"
    assert w.brain.calls == []


# --------------------------------------------------------------------------------------------- session file and controller
def test_session_file_shows_sleeping_state_deadline_and_baselines(tmp_path):
    w = slow_world(tmp_path)
    lock = InstanceLock(tmp_path / "s.db", clock=w.clock)
    lock.acquire(run_id=w.run_id)
    seen: list[dict] = []

    class Spy:
        def sleep(self, seconds):
            seen.append(read_session(tmp_path / "s.db"))
            w.clock.sleep(seconds)
    ctl = attach(w, lock=lock, sleeper=Spy(), ceiling=SessionCeiling(baseline_tokens=7, token_ceiling=250000, baseline_cost=0.5))
    m, _ = raise_429("RelationshipOutput", times=1, retry_after=300, detail=MINUTE)
    w.brain.mutators.append(m)
    try:
        assert w.sup.run().status == "COMPLETED"
        sleeping = [s for s in seen if s["state"] == "SLEEPING"]
        assert sleeping and all(s["sleeping_until"] for s in sleeping)
        assert sleeping[0]["tokens_baseline"] == 7 and sleeping[0]["cost_baseline"] == 0.5 and sleeping[0]["deadline_at"]
        final = read_session(tmp_path / "s.db")
        assert final["state"] == "STOPPING" and final["pid"] == lock.pid
        assert ctl.http_429_minute == 1 == final["http_429_minute"] and final["http_429_day"] == 0
    finally:
        lock.release()


def _stub(settings_kw=None, *, in_tx=False):
    """Controller dependencies without a database (idle_wait / checkpoint only use these)."""
    w_clock = __import__("tests.helpers.clock", fromlist=["FakeClock"]).FakeClock()
    from corpdisc.config import Settings
    ran: list[str] = []
    conn = SimpleNamespace(in_transaction=in_tx, execute=lambda sql: (ran.append(sql), SimpleNamespace(fetchall=lambda: []))[1])
    deps = SimpleNamespace(settings=Settings(_env_file=None, **(settings_kw or {})), clock=w_clock, conn=conn, run_id=1,
                           budget=SimpleNamespace(session=None))
    limiter = SimpleNamespace(rate_limited=0, refusals=0, last_refusal=None, last_429_scope=None, cooldown_remaining=lambda: 0.0)
    return deps, limiter, w_clock, ran


def test_idle_wait_sleeps_in_chunks_and_returns_when_due():
    deps, limiter, clock, _ = _stub()
    ctl = SlowCrawlController(deps, limiter=limiter, sleeper=clock, deadline=clock.monotonic() + 10**6, emit=lambda m: None)
    why, waited = ctl.idle_wait(clock.now() + timedelta(seconds=100))
    assert why is None and 100 <= waited <= 100.1
    assert all(s <= 30 for s in clock.sleeps) and len(clock.sleeps) == 4


def test_idle_wait_refuses_to_sleep_past_the_deadline():
    deps, limiter, clock, _ = _stub()
    ctl = SlowCrawlController(deps, limiter=limiter, sleeper=clock, deadline=clock.monotonic() + 50, emit=lambda m: None)
    assert ctl.idle_wait(clock.now() + timedelta(seconds=100)) == (MAX_RUNTIME, 0.0)
    assert clock.sleeps == []


def test_idle_wait_stops_on_request_and_second_signal():
    deps, limiter, clock, _ = _stub()
    box = {"stop": None}
    ctl = SlowCrawlController(deps, limiter=limiter, sleeper=clock, deadline=clock.monotonic() + 10**6,
                              stop_check=lambda: box["stop"], emit=lambda m: None)
    box["stop"] = "SHUTDOWN"
    assert ctl.idle_wait(clock.now() + timedelta(seconds=100))[0] == INTERRUPTED and clock.sleeps == []

    class Raise:
        def sleep(self, seconds):
            raise KeyboardInterrupt
    box["stop"] = None
    ctl2 = SlowCrawlController(deps, limiter=limiter, sleeper=Raise(), deadline=clock.monotonic() + 10**6, emit=lambda m: None)
    assert ctl2.idle_wait(clock.now() + timedelta(seconds=100))[0] == INTERRUPTED


def test_wal_checkpoint_runs_about_every_ten_minutes_and_never_inside_a_transaction():
    deps, limiter, clock, ran = _stub()
    ctl = SlowCrawlController(deps, limiter=limiter, sleeper=clock, deadline=clock.monotonic() + 10**6, emit=lambda m: None)
    ctl.idle_wait(clock.now() + timedelta(seconds=1300))
    assert ran == ["PRAGMA wal_checkpoint(PASSIVE)"] * 2
    deps2, limiter2, clock2, ran2 = _stub(in_tx=True)
    ctl2 = SlowCrawlController(deps2, limiter=limiter2, sleeper=clock2, deadline=clock2.monotonic() + 10**6, emit=lambda m: None)
    ctl2.idle_wait(clock2.now() + timedelta(seconds=1300))
    assert ran2 == []


def test_rate_limit_delay_never_shortens_the_provider_wait():
    deps, limiter, clock, _ = _stub()
    ctl = SlowCrawlController(deps, limiter=limiter, sleeper=clock, deadline=clock.monotonic() + 10**6, emit=lambda m: None)
    assert ctl.rate_limit_delay(575, hit_429=False) == 575  # no limiter cool-down: the stated Retry-After
    limiter.cooldown_remaining = lambda: 577.0
    assert ctl.rate_limit_delay(575, hit_429=True) == 577.0 and ctl.rate_limit_delay(900, hit_429=False) == 900
    limiter.cooldown_remaining = lambda: 0.0  # already waited out inside the task: only the base delay remains
    assert ctl.rate_limit_delay(575, hit_429=True) == deps.settings.queue_retry_base_seconds


# --------------------------------------------------------------------------------------------- composition root
def _rt(tmp_path, **kw):
    from corpdisc.agents.providers.fake import FakeLLMProvider
    from corpdisc.app import build_runtime
    from corpdisc.config import Settings
    from tests.helpers.clock import FakeClock
    from tests.slow_crawl.test_gateway_limiter import _NoHttp
    clock = FakeClock()
    s = Settings(_env_file=None, db_path=tmp_path / "x.db", cache_dir=tmp_path / ".cache", log_dir=tmp_path / "l",
                 use_playwright="false", llm_provider="groq", groq_model="m", **kw)
    return build_runtime(s, provider=FakeLLMProvider(), http=_NoHttp(), transport=object(), clock=clock, sleeper=clock), clock


def test_build_runtime_wires_the_controller_and_session_ceiling_only_in_slow_mode(tmp_path):
    rt, _ = _rt(tmp_path / "default")
    try:
        assert rt.slow is None and rt.supervisor.slow is None and rt.deps.budget.session is None
    finally:
        rt.close()
    rt, clock = _rt(tmp_path / "slow", slow_crawl=True, slow_crawl_token_ceiling=1234, slow_crawl_cost_ceiling_usd=2.5,
                    llm_price_in_per_mtok=0.5, llm_price_out_per_mtok=1.5)
    try:
        assert rt.slow is rt.supervisor.slow and rt.slow.limiter is rt.limiter
        assert rt.slow.deadline == rt.limiter.deadline == pytest.approx(clock.monotonic() + 24 * 3600)
        ses = rt.deps.budget.session
        assert (ses.baseline_tokens, ses.token_ceiling, ses.cost_ceiling_usd) == (0, 1234, 2.5)
        assert (ses.price_in_per_mtok, ses.price_out_per_mtok) == (0.5, 1.5)
        assert rt.deps.budget._exclude_rate_limited is True
    finally:
        rt.close()


def test_session_baseline_is_read_from_the_runs_row_on_resume(tmp_path):
    rt, _ = _rt(tmp_path, slow_crawl=True)
    run_id = rt.run_id
    rt.conn.execute("UPDATE runs SET llm_tokens_used = 210224, llm_cost_usd = 1.25 WHERE run_id = ?", (run_id,))
    rt.conn.commit()
    rt.close()
    from corpdisc.agents.providers.fake import FakeLLMProvider
    from corpdisc.app import build_runtime
    from corpdisc.config import Settings
    from tests.helpers.clock import FakeClock
    from tests.slow_crawl.test_gateway_limiter import _NoHttp
    s = Settings(_env_file=None, db_path=tmp_path / "x.db", cache_dir=tmp_path / ".cache", log_dir=tmp_path / "l",
                 use_playwright="false", llm_provider="groq", groq_model="m", slow_crawl=True)
    clock = FakeClock()
    rt2 = build_runtime(s, run_id=run_id, provider=FakeLLMProvider(), http=_NoHttp(), transport=object(), clock=clock, sleeper=clock)
    try:
        ses = rt2.deps.budget.session
        assert (ses.baseline_tokens, ses.baseline_cost) == (210224, 1.25)
        assert rt2.deps.budget.session_usage()["tokens"] == 0 and rt2.deps.budget.session_usage()["tokens_remaining"] == 250000
    finally:
        rt2.close()