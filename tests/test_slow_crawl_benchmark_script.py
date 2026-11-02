"""Offline tests of scripts/slow_crawl_benchmark.py: path guards, token ledger, dry-run output, secrecy."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tests.helpers.db import make_db, seed_run

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "slow_crawl_benchmark.py"
spec = importlib.util.spec_from_file_location("slow_crawl_benchmark", SCRIPT)
sb = importlib.util.module_from_spec(spec)
sys.modules["slow_crawl_benchmark"] = sb
spec.loader.exec_module(sb)


def run(tmp_path: Path, *argv: str) -> tuple[int, Path]:
    bench = tmp_path / "bench"
    code = sb.main([*argv, "--bench-dir", str(bench)])
    return code, bench


def only_report(bench: Path) -> dict:
    files = sorted(bench.glob("slow_crawl_benchmark_*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------- protected paths
@pytest.mark.parametrize("name", ["csi_coverage_test.db", "csi_coverage_test.db-wal", "csi_hierarchy.db", "csi_hierarchy.db-shm"])
def test_protected_names_refused_even_inside_bench(tmp_path: Path, name: str) -> None:
    bench = tmp_path / "bench"
    bench.mkdir()
    with pytest.raises(sb.ProtectedPathError):
        sb.assert_benchmark_target(bench / name, bench)


def test_non_benchmark_names_and_locations_refused(tmp_path: Path) -> None:
    bench = tmp_path / "bench"
    bench.mkdir()
    for bad in (bench / "other.db", bench / "sub" / "slow_crawl_x.db", tmp_path / "slow_crawl_x.db", tmp_path / "data" / "csi.db"):
        with pytest.raises(sb.ProtectedPathError):
            sb.assert_benchmark_target(bad, bench)
    assert sb.assert_benchmark_target(bench / "slow_crawl_micro_1.db", bench).name == "slow_crawl_micro_1.db"


def test_backups_directory_refused(tmp_path: Path) -> None:
    backups = tmp_path / "data" / "backups"
    backups.mkdir(parents=True)
    with pytest.raises(sb.ProtectedPathError):
        sb.assert_benchmark_target(backups / "slow_crawl_x.db", backups)
    with pytest.raises(sb.ProtectedPathError):
        sb.assert_source_readable(backups / "anything.db")


def test_source_guard_refuses_live_db_but_allows_coverage_db(tmp_path: Path) -> None:
    live = tmp_path / "csi_hierarchy.db"
    live.write_bytes(b"x")
    with pytest.raises(sb.ProtectedPathError):
        sb.assert_source_readable(live)
    cov = tmp_path / "csi_coverage_test.db"
    cov.write_bytes(b"x")
    assert sb.assert_source_readable(cov) == cov.resolve()  # readable, but only ever through a mode=ro backup copy


def test_main_refuses_when_bench_dir_is_backups(tmp_path: Path, capsys) -> None:
    target = tmp_path / "data" / "backups"
    code = sb.main(["micro", "--dry-run", "--bench-dir", str(target)])
    assert code == sb.EXIT_USAGE
    assert "refused" in capsys.readouterr().err
    assert not list(target.glob("*.db"))


def test_new_db_never_reuses_existing_file(tmp_path: Path) -> None:
    bench = tmp_path / "bench"
    bench.mkdir()
    (bench / "slow_crawl_micro_S.db").write_bytes(b"keep")
    ctx = sb.Context(mode="micro", dry_run=True, bench_dir=bench, settings=None, clock=sb.VirtualClock(),
                     ledger=sb.TokenLedger(bench / sb.LEDGER_NAME, persist=False), stamp="S", inner_transport=None, api_key="")
    with pytest.raises(sb.ProtectedPathError):
        sb.new_db(ctx, "micro")
    assert (bench / "slow_crawl_micro_S.db").read_bytes() == b"keep"


# ------------------------------------------------------------------------------------------- ledger
def test_ledger_starts_at_178_and_persists(tmp_path: Path) -> None:
    path = tmp_path / sb.LEDGER_NAME
    led = sb.TokenLedger(path)
    assert led.total == 178
    res = led.reserve(500, "t")
    led.settle(res, 420)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["total_tokens"] == 598
    assert sb.TokenLedger(path).total == 598


def test_ledger_refuses_request_that_could_pass_the_cap(tmp_path: Path) -> None:
    path = tmp_path / sb.LEDGER_NAME
    path.write_text(json.dumps({"total_tokens": 119_000}), encoding="utf-8")
    led = sb.TokenLedger(path)
    led.reserve(900, "ok")
    with pytest.raises(sb.LedgerRefused):
        led.reserve(200, "too much")  # 119,000 + 900 reserved + 200 > 120,000
    assert led.total == 119_000


def test_ledger_caps_never_exceed_hard_cap(tmp_path: Path) -> None:
    led = sb.TokenLedger(tmp_path / sb.LEDGER_NAME, persist=False, soft_cap=999_999)
    assert led.soft_cap == sb.HARD_CAP == 150_000
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"total_tokens": 150_001}), encoding="utf-8")
    with pytest.raises(sb.LedgerError):
        sb.TokenLedger(bad)


def test_scope_cap_applies_below_the_global_cap(tmp_path: Path) -> None:
    led = sb.TokenLedger(tmp_path / sb.LEDGER_NAME, persist=False)
    with led.scope("m", 1_000):
        led.settle(led.reserve(600, "a"), 600)
        with pytest.raises(sb.LedgerRefused):
            led.reserve(401, "b")
        led.reserve(400, "c")


def test_ledger_transport_sends_nothing_when_refused(tmp_path: Path) -> None:
    class Inner:
        calls = 0

        def post(self, *a, **k):
            Inner.calls += 1

    led = sb.TokenLedger(tmp_path / sb.LEDGER_NAME, persist=False, soft_cap=300)
    tr = sb.LedgerTransport(Inner(), led, "x")
    body = json.dumps({"messages": [{"content": "a" * 4000}], "max_completion_tokens": 100})
    with pytest.raises(sb.LedgerRefused):
        tr.post("https://x", headers={}, body=body, timeout=1)
    assert Inner.calls == 0


def test_micro_stops_when_ledger_is_nearly_full(tmp_path: Path, capsys) -> None:
    bench = tmp_path / "bench"
    bench.mkdir()
    (bench / sb.LEDGER_NAME).write_text(json.dumps({"total_tokens": 119_900}), encoding="utf-8")
    code, _ = run(tmp_path, "micro", "--dry-run")
    assert code == sb.EXIT_STOPPED
    rep = only_report(bench)
    assert rep["stopped_reason"]
    assert len(rep["results"]["requests"]) < 9
    assert "stopped" in capsys.readouterr().err
    assert json.loads((bench / sb.LEDGER_NAME).read_text(encoding="utf-8"))["total_tokens"] == 119_900  # dry run never writes


# ------------------------------------------------------------------------------------------- dry runs
def test_micro_dry_run_report_shape_and_pacing(tmp_path: Path) -> None:
    code, bench = run(tmp_path, "micro", "--dry-run")
    assert code == sb.EXIT_OK
    rep = only_report(bench)
    assert rep["mode"] == "micro" and rep["dry_run"] is True and rep["configured_model_changed"] is False
    assert rep["groq_api_key_present"] is False
    res = rep["results"]
    assert res["interval_s"] == 20.0
    assert len(res["requests"]) == 9 and all(r["ok"] for r in res["requests"])
    assert res["gaps_respect_interval"] is True and res["min_gap_s"] >= 19.5
    assert res["one_in_flight"] is True and res["max_in_flight"] == 1
    assert res["each_request_within_120_tokens"] is True
    fi = res["fault_injection"]
    assert fi["synthetic_429_injected"] == 1 and fi["request_recovered"] is True
    assert fi["wait_respects_retry_after"] is True and fi["real_http_429"] == 0
    assert rep["ledger"]["persisted"] is False and rep["ledger"]["soft_cap"] == 120_000


def test_models_dry_run_report_shape(tmp_path: Path) -> None:
    code, bench = run(tmp_path, "models", "--dry-run", "--price", "openai/gpt-oss-20b=0.1,0.5")
    assert code == sb.EXIT_OK
    res = only_report(bench)["results"]
    assert res["sample_count"] == 3
    assert list(res["models"]) == list(sb.DEFAULT_MODELS)
    for m in res["models"].values():
        for key in ("schema_valid_rate", "emitted", "grounded", "precision", "recall", "tokens_in", "tokens_out", "latency_ms",
                    "http_429", "cost_usd", "grounded_corporate_entities", "samples"):
            assert key in m
    q, big, small = (res["models"][k] for k in sb.DEFAULT_MODELS)
    assert q["precision"] == 1.0 and q["recall"] == 1.0
    assert big["emitted"] == big["grounded"] + 1 and big["precision"] < 1.0  # one ungrounded company is not counted as grounded
    assert small["schema_valid_rate"] < 1.0 and small["cost_usd"] > 0 and q["cost_usd"] == 0


def test_dry_run_writes_only_benchmark_files_and_no_ledger(tmp_path: Path) -> None:
    for mode in ("micro", "models"):
        code, bench = run(tmp_path, mode, "--dry-run")
        assert code == sb.EXIT_OK
    names = {p.name for p in bench.iterdir() if p.is_file()}
    assert names and all(n.startswith("slow_crawl_") for n in names)
    assert sb.LEDGER_NAME not in names
    assert [p for p in tmp_path.rglob("*") if p.is_file() and "bench" not in p.parts and p.suffix in (".db", ".json")] == []


def test_dry_run_makes_no_network_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise AssertionError("network used in dry run")

    monkeypatch.setattr(sb.RequestsJsonTransport, "post", boom)
    for mode in ("micro", "models"):
        assert run(tmp_path, mode, "--dry-run")[0] == sb.EXIT_OK


def test_live_mode_without_key_sends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(sb, "load_settings", lambda env_file=None: sb.Settings(_env_file=None))
    code, bench = run(tmp_path, "micro")
    assert code == sb.EXIT_USAGE
    assert "GROQ_API_KEY" in capsys.readouterr().err
    assert not list(bench.glob("*.db"))


def test_api_key_never_appears_in_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    canary = "gsk_CANARY_0123456789abcdefSECRET"
    monkeypatch.setenv("GROQ_API_KEY", canary)
    for mode in ("micro", "models"):
        assert run(tmp_path, mode, "--dry-run")[0] == sb.EXIT_OK
    captured = capsys.readouterr()
    assert canary not in captured.out and canary not in captured.err
    for p in (tmp_path / "bench").iterdir():
        if p.suffix == ".json":
            assert canary not in p.read_text(encoding="utf-8")
    assert sb.scrub(f"Bearer {canary}", [canary]) == "Bearer [REDACTED]"


# ------------------------------------------------------------------------------------------- read-only copy and samples
def test_readonly_copy_leaves_source_untouched_and_yields_samples(tmp_path: Path) -> None:
    conn = make_db(tmp_path, "csi_coverage_test.db")
    run_id = seed_run(conn)
    inp = sb.synthetic_inputs()[0]
    stored = {"payload": {"entities": [{"name": "Acme Software Inc.", "claim": {"quote": sb.SYNTH_PAGES[0]["entities"][0][1]}}]}}
    conn.execute(
        "INSERT INTO agent_invocations(run_id, agent, correlation_id, schema_version, prompt_version, attempt, input_hash, "
        "request_json, response_json, status, cache_hit, tokens_in, tokens_out, started_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, "extraction", "c", "1", "1", 1, "h", json.dumps({"attempt_kind": "INITIAL", "input": inp.model_dump(mode="json")}),
         json.dumps(stored), "OK", 0, 900, 50, "2026-01-01T00:00:00+00:00"))
    conn.commit()
    conn.close()
    src = tmp_path / "data" / "csi_coverage_test.db"
    before = (src.stat().st_size, src.stat().st_mtime_ns, src.read_bytes())
    dest = tmp_path / "bench" / "slow_crawl_models_source_T.db"
    dest.parent.mkdir()
    sb.make_readonly_copy(src, dest)
    after = (src.stat().st_size, src.stat().st_mtime_ns, src.read_bytes())
    assert before == after
    samples = sb.load_samples(dest, 3)
    assert len(samples) == 1 and samples[0]["input"].page.page_id == inp.page.page_id
    assert samples[0]["stored"][0][0] == "Acme Software Inc."


def test_grounding_uses_quote_verifier() -> None:
    v = sb.QuoteVerifier()
    text = "Acme Software Inc. is a wholly owned subsidiary of Example Holdings."
    got = sb.grounded_names([("Acme Software Inc.", "Acme Software Inc. is a wholly owned subsidiary"),
                             ("Ghost Co", "Ghost Co is owned by Example Holdings entirely")], text, v)
    assert got == {"acme software inc"}
