"""CLI run/report/llms behaviour: exit codes per run status, Ollama installed-model enforcement, accurate report."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import corpdisc.app as app
import corpdisc.cli as cli
from corpdisc.cli import main
from corpdisc.graphs.supervisor import RunSummary
from corpdisc.llm_registry import OllamaProbe
from tests.fixtures.site import page


@pytest.fixture
def env(tmp_path, monkeypatch, local_site):
    for k, v in {"START_URL": local_site.url("/"), "ROOT_COMPANY": "Northwind Holdings", "ROOT_CODE": "NWH",
                 "DB_PATH": str(tmp_path / "run.db"), "CACHE_DIR": str(tmp_path / ".cache"),
                 "LOG_DIR": str(tmp_path / "logs"), "REQUEST_DELAY_SECONDS": "0", "MAX_RETRIES": "0",
                 "ALLOW_PRIVATE_HOSTS": "true", "USE_PLAYWRIGHT": "false", "RESPECT_ROBOTS": "false",
                 "LLM_PROVIDER": "", "LLM_MODEL": "", "GROQ_API_KEY": "", "GEMINI_API_KEY": ""}.items():
        monkeypatch.setenv(k, v)
    return local_site


def fake_runtime(status, reason="QUEUE_EMPTY"):
    class Sup:
        def seed_root(self): ...
        def run(self, max_tasks=None): return RunSummary(status=status, stopped_reason=reason)
        resume = run

    return SimpleNamespace(run_id=1, supervisor=Sup(), close=lambda: None)


@pytest.mark.parametrize(("status", "code"), [("COMPLETED", 0), ("PARTIAL", 4), ("FAILED", 1), ("PAUSED", 3), ("RUNNING", 3),
                                              ("BUDGET_EXHAUSTED", 3)])
def test_exit_code_reflects_run_status(env, monkeypatch, capsys, status, code):
    monkeypatch.setattr(app, "build_runtime", lambda *a, **k: fake_runtime(status))
    assert main(["run", "--provider", "fake", "--model", "m"]) == code
    out = capsys.readouterr().out
    assert ("BUDGET_STOPPED" if status == "BUDGET_EXHAUSTED" else status) in out


def test_partial_run_is_not_reported_as_complete(env, monkeypatch, capsys):
    monkeypatch.setattr(app, "build_runtime", lambda *a, **k: fake_runtime("PARTIAL"))
    main(["run", "--provider", "fake", "--model", "m"])
    out = capsys.readouterr().out
    assert "COMPLETED" not in out and "gaps" in out


def test_ollama_model_that_is_not_installed_is_rejected_before_anything_runs(env, monkeypatch, capsys):
    probe = OllamaProbe(True, ("mistral:latest",), "", "http://localhost:11434")
    monkeypatch.setattr(cli, "discover_ollama", lambda *a, **k: probe)
    monkeypatch.setattr(app, "build_runtime", lambda *a, **k: pytest.fail("must not start"))
    assert main(["run", "--provider", "ollama", "--model", "qwen3:8b"]) == 2
    err = capsys.readouterr().err
    assert "not installed" in err and "mistral:latest" in err and "ollama pull qwen3:8b" in err
    assert env.total_hits == 0


def test_ollama_server_down_is_rejected(env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "discover_ollama", lambda *a, **k: OllamaProbe(False, (), "ConnectError", "http://localhost:11434"))
    assert main(["run", "--provider", "ollama", "--model", "mistral:latest"]) == 2
    assert "not reachable" in capsys.readouterr().err


def test_installed_ollama_model_starts_with_that_model(env, monkeypatch):
    monkeypatch.setattr(cli, "discover_ollama", lambda *a, **k: OllamaProbe(True, ("mistral:latest",), "", "u"))
    seen = {}

    def rt(settings, **kw):
        seen.update(kw)
        return fake_runtime("COMPLETED")

    monkeypatch.setattr(app, "build_runtime", rt)
    assert main(["run", "--provider", "ollama", "--model", "mistral:latest"]) == 0
    assert seen["run_provider"] == "ollama" and seen["run_model"] == "mistral:latest"


def test_llms_command_lists_only_installed_ollama_models(env, monkeypatch, capsys):
    import corpdisc.llm_registry as reg

    monkeypatch.setattr(reg, "_default_fetch", lambda url, timeout: (200, {"models": [{"name": "mistral:latest"}, {"name": "qwen3:4b"}]}))
    assert main(["llms"]) == 0
    out = capsys.readouterr().out
    assert "mistral:latest" in out and "qwen3:4b" in out and "qwen3:8b" not in out and "gemini-3.8" in out
    assert "[-] Gemini" in out and "[1] Ollama" in out


def test_report_shows_real_status_provider_and_per_agent_counts(env, tmp_path, monkeypatch, capsys):
    env.add("/", page("Northwind Holdings", "<p>Leaf.</p>"))
    assert main(["run"]) == 0
    capsys.readouterr()
    conn = cli._stack(cli._load(setup=False), 1, write=True)[0]
    conn.execute("INSERT INTO agent_invocations (run_id, task_id, agent, correlation_id, schema_version, prompt_version, attempt, "
                 "input_hash, request_json, status, failure_kind, cache_hit, tokens_in, tokens_out, started_at) VALUES "
                 "(1, NULL, 'relationship_verification', 'c1', '1', '1', 1, 'h', '{}', 'FAILED', 'TRANSPORT', 0, 10, 0, '2026-01-01T00:00:00+00:00'),"
                 "(1, NULL, 'relationship_verification', 'c2', '1', '1', 1, 'h2', '{}', 'OK', NULL, 0, 100, 50, '2026-01-01T00:00:01+00:00')")
    conn.commit()
    conn.close()
    assert main(["report", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["run"]["status"] == "COMPLETED" and rep["run"]["status_label"] == "COMPLETED"
    rel = rep["llm"]["per_agent"]["relationship_verification"]
    assert (rel["ok"], rel["failed"], rel["failure_rate"], rel["failure_kinds"]) == (1, 1, 0.5, {"TRANSPORT": 1})
    assert (rel["tokens_in"], rel["tokens_out"]) == (110, 50)
    assert main(["report"]) == 0
    text = capsys.readouterr().out
    assert "[COMPLETED]" in text and "fail_rate=0.5" in text and "provider=" in text
