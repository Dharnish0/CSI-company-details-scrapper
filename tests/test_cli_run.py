"""CLI run / resume / report: provider safety, pause semantics, and the calibration report, all on a loopback site."""
from __future__ import annotations

import json

import pytest

from corpdisc.cli import main
from corpdisc.db.connection import connect_readonly
from tests.fixtures.site import S, page


@pytest.fixture
def env(tmp_path, monkeypatch, local_site):
    """Isolated configuration pointing at a loopback site; no provider configured."""
    for k, v in {"START_URL": local_site.url("/"), "ROOT_COMPANY": "Northwind Holdings", "ROOT_CODE": "NWH",
                 "DB_PATH": str(tmp_path / "run.db"), "CACHE_DIR": str(tmp_path / ".cache"),
                 "LOG_DIR": str(tmp_path / "logs"), "REQUEST_DELAY_SECONDS": "0", "MAX_RETRIES": "0",
                 "ALLOW_PRIVATE_HOSTS": "true", "USE_PLAYWRIGHT": "false", "RESPECT_ROBOTS": "false"}.items():
        monkeypatch.setenv(k, v)
    return local_site


def listing(site):
    site.add("/", page("Northwind Holdings", "<p>Welcome.</p>", nav="<a href='/companies'>Our Companies</a>"))
    site.add("/companies", page("Our Companies", f"<p>{S['ga']}</p>", nav="<a href='/companies'>Our Companies</a>"))


def status(tmp_path):
    c = connect_readonly(tmp_path / "run.db")
    try:
        return c.execute("SELECT status FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()[0]
    finally:
        c.close()


def test_run_without_a_provider_pauses_and_does_not_crash(env, tmp_path, capsys):
    listing(env)
    assert main(["run"]) == 3
    out = capsys.readouterr().out
    assert "PAUSED" in out and "CIRCUIT_OPEN" in out and "python main.py resume" in out
    assert status(tmp_path) == "PAUSED"
    assert main(["resume"]) == 3  # still no provider: stays paused, never loops or crashes
    assert main(["report"]) == 0
    rep = capsys.readouterr().out
    assert "Run 1 [PAUSED]" in rep and "integrity" in rep


def test_run_completes_when_no_llm_is_needed(env, tmp_path, capsys):
    env.add("/", page("Northwind Holdings", "<p>A leaf site with nothing to list.</p>"))
    assert main(["run"]) == 0
    out = capsys.readouterr().out
    assert "COMPLETED" in out and "Finalize: CLEAN" in out and status(tmp_path) == "COMPLETED"


def test_real_provider_requires_explicit_confirmation_and_builds_no_transport(env, monkeypatch, capsys):
    import corpdisc.app as app

    def boom(*a, **k):
        raise AssertionError("a real transport must not be constructed")

    monkeypatch.setattr(app, "RequestsJsonTransport", boom)
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_KEY", "sk-test-SECRETSECRET123456")
    monkeypatch.setenv("LLM_MODEL", "some-model")
    assert main(["run"]) == 2
    err = capsys.readouterr().err
    assert "without --yes" in err and "sk-test" not in err
    assert env.total_hits == 0  # nothing was fetched either


def test_real_provider_needs_a_model_even_with_yes(env, monkeypatch, capsys):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "sk-test-SECRETSECRET123456")
    assert main(["run", "--yes"]) == 2
    err = capsys.readouterr().err
    assert "LLM_MODEL is required" in err and "sk-test" not in err


def test_resume_with_no_run_and_report_with_no_run(env, capsys):
    assert main(["resume"]) == 2 and "No run to resume" in capsys.readouterr().err
    assert main(["report"]) == 2


def test_max_tasks_leaves_the_run_resumable(env, tmp_path, capsys):
    listing(env)
    assert main(["run", "--max-tasks", "0"]) == 3
    assert "MAX_TASKS" in capsys.readouterr().out and status(tmp_path) == "RUNNING"


def test_report_json_is_valid_and_secret_free(env, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LLM_API_KEY", "sk-test-SECRETSECRET123456")
    env.add("/", page("Northwind Holdings", "<p>Leaf.</p>"))
    main(["run"])
    capsys.readouterr()
    assert main(["report", "--json"]) == 0
    raw = capsys.readouterr().out
    rep = json.loads(raw)
    assert rep["run"]["status"] == "COMPLETED" and rep["integrity"]["confirmed_relationships_without_verified_evidence"] == 0
    assert "sk-test" not in raw
