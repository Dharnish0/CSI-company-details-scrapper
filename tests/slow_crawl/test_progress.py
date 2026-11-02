"""Progress service and `progress` command: exact numbers on a seeded database, read-only behaviour, JSON output."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from corpdisc.clock import iso_utc
from corpdisc.cli import main
from corpdisc.config import Settings
from corpdisc.db.connection import connect_readonly
from corpdisc.services.calibration import build_report, format_report
from corpdisc.services.progress import build_progress, format_progress
from tests.helpers.db import T0, make_db, seed_entity, seed_page, seed_relationship, seed_run

NOW = T0 + timedelta(hours=2)


def settings(**kw) -> Settings:
    return Settings(_env_file=None, max_total_pages=10, max_llm_calls_per_run=20, **kw)


def _task(conn, run, entity, status, *, depth=1, not_before=None, error=None, attempts=1):
    conn.execute(
        "INSERT INTO research_queue(run_id, entity_id, start_url, depth, priority, status, attempts, enqueued_at, not_before, error) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run, entity, None, depth, depth * 1_000_000 + entity, status, attempts, iso_utc(T0),
         iso_utc(not_before) if not_before else None, error))
    conn.commit()


def _inv(conn, run, *, status="OK", kind=None, cache_hit=0, body=None, tokens=(100, 50)):
    conn.execute(
        "INSERT INTO agent_invocations(run_id, agent, correlation_id, schema_version, prompt_version, input_hash, request_json, "
        "response_json, status, failure_kind, cache_hit, tokens_in, tokens_out, started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run, "extraction", "c", "1", "1", f"h{conn.total_changes}", "{}", None if body is None else json.dumps(body), status, kind,
         cache_hit, tokens[0], tokens[1], iso_utc(T0)))
    conn.commit()


@pytest.fixture
def seeded(tmp_path):
    conn = make_db(tmp_path)
    run = seed_run(conn, root_name="Root Holdings", root_code="RH")
    root = seed_entity(conn, run, "Root Holdings", entity_type="Parent Company")
    og = seed_entity(conn, run, "Op Group", entity_type="Operating Group")
    port_a = seed_entity(conn, run, "Port A", entity_type="Portfolio Company")
    port_b = seed_entity(conn, run, "Port B", entity_type="Portfolio Company", status="REVIEW_REQUIRED")
    sub_x = seed_entity(conn, run, "Sub X", entity_type="Company")
    sub_z = seed_entity(conn, run, "Sub Z", entity_type="Company")
    bad = seed_entity(conn, run, "Bad Co", status="REJECTED")
    dup = seed_entity(conn, run, "Dup", merged_into=port_a)
    seed_relationship(conn, root, og)
    seed_relationship(conn, og, port_a)
    seed_relationship(conn, port_a, sub_x)  # root -> og -> port_a -> sub_x : three levels below the root
    seed_relationship(conn, og, port_b, status="REVIEW_REQUIRED")
    seed_relationship(conn, port_b, sub_z)  # confirmed edge under an unconfirmed parent: not part of the tree
    _task(conn, run, root, "DONE", depth=0)
    _task(conn, run, og, "DONE")
    _task(conn, run, port_a, "DONE", depth=2, error="PARTIAL:RETRYABLE:X")
    _task(conn, run, port_b, "PENDING", depth=2, attempts=0)
    _task(conn, run, sub_x, "PENDING", depth=3, attempts=0, not_before=NOW + timedelta(seconds=600))
    _task(conn, run, sub_z, "RUNNING", depth=3)
    _task(conn, run, bad, "FAILED", depth=3, attempts=3)
    _task(conn, run, dup, "SKIPPED", depth=3)
    _inv(conn, run, body={"transport": {"requests": 1, "errors": []}})
    _inv(conn, run, cache_hit=1, body={"cached_from": 1}, tokens=(0, 0))
    _inv(conn, run, status="FAILED", kind="TRANSPORT", tokens=(0, 0),
         body={"error": {"kind": "TRANSPORT"}, "transport": {"requests": 0, "errors": ["RATE_LIMITED", "RATE_LIMITED"]}})
    _inv(conn, run, body={"transport": {"requests": 2, "errors": ["RATE_LIMITED"]}})
    _inv(conn, run, status="FAILED", kind="SCHEMA", body={"transport": {"requests": 1, "errors": []}})
    _inv(conn, run, status="SKIPPED", kind="BUDGET", tokens=(0, 0))
    for i in range(3):
        seed_page(conn, run, f"https://example.test/{i}")
    conn.execute("UPDATE runs SET llm_tokens_used = 210224, llm_cost_usd = 0 WHERE run_id = ?", (run,))
    conn.commit()
    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.close()
    return path, run


def _progress(path, run, *, session=None, **kw):
    conn = connect_readonly(path)
    try:
        return build_progress(conn, run, settings(**kw), now=NOW, session=session)
    finally:
        conn.close()


def test_exact_numbers_without_session(seeded):
    path, run = seeded
    p = _progress(path, run)
    assert p["entities"] == {"total": 7, "confirmed": 5, "review_required": 1, "rejected": 1,
                             "by_type": {"Parent Company": 1, "Operating Group": 1, "Portfolio Company": 2, "Company": 3}}
    assert p["groups"] == {"operating_groups": {"discovered": 1, "processed": 1},
                           "portfolio_companies": {"discovered": 2, "processed": 1}}
    assert p["hierarchy"] == {"max_depth": 3, "levels": 4, "max_task_depth": 3}  # 3-level chain below the root
    assert p["tasks"] == {"total": 8, "done": 3, "pending": 1, "waiting_retry": 1, "running": 1, "failed": 1, "skipped": 1,
                          "done_with_gaps": 1}
    assert p["next_retry"] == {"at": iso_utc(NOW + timedelta(seconds=600)), "in_seconds": 600.0}
    a = p["api"]
    assert (a["invocations"], a["successful_calls"], a["failed_calls"], a["cache_hits"], a["http_429"]) == (6, 2, 2, 1, 3)
    assert a["requests"] == 1 + 2 + 1 + 3  # provider-reaching requests plus the three refused with HTTP 429
    assert a["http_429_day"] is None and a["http_429_minute"] is None
    assert p["usage"]["tokens"] == 210224 and p["usage"]["prices_set"] is False and "prices unset" in p["usage"]["cost_note"]
    assert p["crawler"]["state"] == "IDLE" and p["crawler"]["pid"] is None
    b = p["budgets"]
    assert b["pages"] == {"used": 3, "limit": 10, "remaining": 7}
    assert b["llm_calls"] == {"used": 4, "limit": 20, "remaining": 16}  # default rule: 429-only failures still count
    assert b["run_tokens"] == {"budget": None, "remaining": None}
    assert b["session_tokens"] is None and b["session_cost"] is None and b["runtime"] is None


def test_session_adds_state_split_429_and_remaining_budgets(seeded):
    path, run = seeded
    sess = {"pid": os.getpid(), "state": "SLEEPING", "sleeping_until": iso_utc(NOW + timedelta(seconds=300)),
            "heartbeat": iso_utc(NOW - timedelta(seconds=12)), "started_at": iso_utc(NOW - timedelta(hours=1)),
            "tokens_baseline": 200_000, "cost_baseline": 0.0, "http_429_day": 1, "http_429_minute": 2, "last_429_scope": "day"}
    p = _progress(path, run, session=sess, slow_crawl=True, slow_crawl_cost_ceiling_usd=1.0)
    c = p["crawler"]
    assert c["state"] == f"SLEEPING until {sess['sleeping_until']}" and c["pid_alive"] is True
    assert c["heartbeat_age_seconds"] == 12.0
    assert (p["api"]["http_429_day"], p["api"]["http_429_minute"], p["api"]["last_429_scope"]) == (1, 2, "day")
    b = p["budgets"]
    assert b["llm_calls"]["used"] == 3  # slow mode: the 429-only failure no longer counts toward the run cap
    assert b["session_tokens"] == {"used": 10224, "ceiling": 250_000, "remaining": 239_776}
    assert b["session_cost"] == {"used": 0.0, "ceiling": 1.0, "remaining": 1.0, "active": False}
    assert b["runtime"] == {"deadline": iso_utc(NOW + timedelta(hours=23)), "remaining_seconds": 23 * 3600.0}
    text = format_progress(p)
    for needle in ("SLEEPING until", "companies discovered: 7", "confirmed: 5", "review-required: 1", "operating groups: 1 found",
                   "portfolio companies: 2 found", "hierarchy depth: 3", "3 done, 1 pending, 1 waiting for retry", "7 requests",
                   "2 successful calls", "1 cache hits", "3 HTTP 429 (daily 1, per-minute 2)", "tokens: 210224", "prices unset",
                   "next scheduled retry:", "remaining pages: 7 of 10", "LLM calls: 17 of 20", "session token ceiling: 250000",
                   "session cost ceiling", "runtime remaining: 23h00m00s"):
        assert needle in text, needle


def test_dead_pid_session_is_reported_stale(seeded):
    path, run = seeded
    import subprocess
    import sys
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    p = _progress(path, run, session={"pid": proc.pid, "state": "RUNNING", "started_at": iso_utc(NOW)})
    assert p["crawler"]["state"].startswith("STALE") and p["crawler"]["pid_alive"] is False
    assert p["budgets"]["runtime"] is None


def test_cost_with_prices_and_run_token_budget(seeded):
    path, run = seeded
    conn = sqlite3.connect(path)
    conn.execute("UPDATE runs SET llm_token_budget = 300000, llm_cost_usd = 0.42 WHERE run_id = ?", (run,))
    conn.commit()
    conn.close()
    p = _progress(path, run, llm_price_in_per_mtok=1.0, llm_price_out_per_mtok=2.0)
    assert p["usage"] == {"tokens": 210224, "cost_usd": 0.42, "prices_set": True, "cost_note": None}
    assert p["budgets"]["run_tokens"] == {"budget": 300000, "remaining": 89776}
    assert "prices unset" not in format_progress(p)


def test_chain_depth_is_bounded_on_a_cycle(tmp_path):
    conn = make_db(tmp_path)
    run = seed_run(conn)
    a = seed_entity(conn, run, "A")
    b = seed_entity(conn, run, "B")
    seed_relationship(conn, a, b)
    seed_relationship(conn, b, a)  # a cycle that the committer would reject; progress must still terminate
    conn.close()
    p = _progress(Path(tmp_path / "data" / "test.db"), run)
    assert p["hierarchy"]["max_depth"] == 64


def test_empty_run_and_unknown_run(tmp_path):
    conn = make_db(tmp_path)
    run = seed_run(conn)
    conn.close()
    p = _progress(tmp_path / "data" / "test.db", run)
    assert p["entities"]["total"] == 0 and p["hierarchy"]["max_depth"] == 0 and p["tasks"]["total"] == 0
    assert p["next_retry"] == {"at": None, "in_seconds": None}
    assert "next scheduled retry: none" in format_progress(p)
    with pytest.raises(LookupError):
        _progress(tmp_path / "data" / "test.db", 999)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_progress_leaves_database_bytes_unchanged_and_connection_is_read_only(seeded):
    path, run = seeded
    before = _digest(path)
    conn = connect_readonly(path)
    try:
        build_progress(conn, run, settings(), now=NOW, session=None)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO crawl_errors(run_id, stage, error_type, message, occurred_at) VALUES (1,'X','Y','z','t')")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE runs SET llm_tokens_used = 0")
    finally:
        conn.close()
    assert _digest(path) == before


def test_calibration_report_has_http_429_and_format_is_unchanged(seeded):
    path, run = seeded
    conn = connect_readonly(path)
    try:
        rep = build_report(conn, run)
    finally:
        conn.close()
    assert rep["llm"]["http_429"] == 3
    assert "429" not in format_report(rep)  # the text report keeps its existing layout


# ------------------------------------------------------------------------------------------------------------- CLI
@pytest.fixture
def cli_env(seeded, tmp_path, monkeypatch):
    path, run = seeded
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    return path, run


def test_cli_progress_json_parses(cli_env, capsys):
    path, run = cli_env
    before = _digest(path)
    assert main(["progress", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["run"]["run_id"] == run and data["entities"]["total"] == 7 and data["hierarchy"]["max_depth"] == 3
    assert data["api"]["http_429"] == 3 and data["crawler"]["state"] == "IDLE"
    assert _digest(path) == before


def test_cli_progress_text_and_run_id(cli_env, capsys):
    _, run = cli_env
    assert main(["progress", "--run-id", str(run)]) == 0
    out = capsys.readouterr().out
    assert "companies discovered: 7" in out and "crawler: IDLE" in out
    assert main(["progress", "--run-id", "999"]) == 2
    assert "unknown run 999" in capsys.readouterr().err


def test_cli_progress_does_not_create_a_missing_database(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "nothing.db"
    monkeypatch.setenv("DB_PATH", str(missing))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    assert main(["progress"]) == 2
    assert not missing.exists() and "not found" in capsys.readouterr().err


def test_cli_progress_watch_stops_on_ctrl_c_with_exit_0(cli_env, capsys, monkeypatch):
    from corpdisc.clock import SystemClock
    slept: list[float] = []

    def fake_sleep(self, seconds):
        slept.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(SystemClock, "sleep", fake_sleep)
    assert main(["progress", "--watch", "2.5"]) == 0
    assert slept == [2.5] and "companies discovered" in capsys.readouterr().out
    assert main(["progress", "--watch", "0"]) == 2


def test_progress_help_lists_options(capsys):
    with pytest.raises(SystemExit) as ei:
        main(["progress", "--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "--run-id" in out and "--watch" in out and "--json" in out
