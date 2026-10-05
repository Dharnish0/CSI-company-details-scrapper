"""M8 guarantees: ordinary tests can never reach a real LLM, replay fails closed, no secrets/artifacts are tracked."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from corpdisc.agents.ports import ProviderRequest, ProviderTransportError
from corpdisc.agents.providers import ReplayProvider, UnconfiguredProvider, build_provider_from_settings
from corpdisc.config import Settings
from corpdisc.services.llm_http import RequestsJsonTransport
from corpdisc.transport import TransportFailure

ROOT = Path(__file__).resolve().parents[2]
SRC, TESTS = ROOT / "src" / "corpdisc", ROOT / "tests"
MSG = ProviderRequest(model="m", system="s", messages=(), response_schema={}, schema_name="X", temperature=0.0, max_output_tokens=1)


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


# ------------------------------------------------------------------ real provider is unreachable from ordinary tests
def test_live_tests_are_deselected_by_default():
    out = subprocess.run([sys.executable, "-m", "pytest", "tests/live", "--collect-only", "-q", "-p", "no:cacheprovider"],
                         cwd=ROOT, capture_output=True, text=True, timeout=120)
    text = out.stdout + out.stderr
    assert "deselected" in text and "::test_" not in text  # collected nothing, even if keys exist in the environment


def test_real_transport_is_constructed_only_in_the_composition_root():
    users = [p.relative_to(SRC).as_posix() for p in SRC.rglob("*.py") if "RequestsJsonTransport(" in p.read_text(encoding="utf-8")]
    assert users == ["app.py"]
    allowed = {"tests/services/test_llm_http.py", "tests/unit/test_m8_guards.py"}
    in_tests = [p.relative_to(ROOT).as_posix() for p in TESTS.rglob("*.py")
                if "RequestsJsonTransport(" in p.read_text(encoding="utf-8") and "tests/live/" not in p.as_posix()]
    assert set(in_tests) <= allowed


def test_the_network_guard_blocks_the_real_api_in_ordinary_tests():
    with pytest.raises((RuntimeError, TransportFailure)):
        RequestsJsonTransport(trust_env=False).post("https://api.anthropic.com/v1/messages", headers={"x-api-key": "k"},
                                                    body="{}", timeout=2)


def test_default_configuration_has_no_provider_and_no_key():
    st = Settings(_env_file=None)
    assert st.llm_provider == "" and st.llm_api_key is None and st.search_api_key is None
    p = build_provider_from_settings(st)
    assert isinstance(p, UnconfiguredProvider)
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    for var in ("LLM_API_KEY", "SEARCH_API_KEY", "LLM_PROVIDER"):
        assert re.search(rf"^{var}=\s*$", env, re.M), f"{var} must be blank in .env.example"


def test_replay_fails_closed_and_never_falls_through_to_a_live_provider(tmp_path):
    st = Settings(_env_file=None, llm_provider="replay", llm_api_key="sk-test-whatever-123456", cache_dir=tmp_path / "c",
                  llm_cassette_dir=tmp_path / "missing", db_path=tmp_path / "x.db", log_dir=tmp_path / "l")
    p = build_provider_from_settings(st)  # even with a key present, replay is replay
    assert isinstance(p, ReplayProvider)
    with pytest.raises(ProviderTransportError, match="cassette miss") as e:
        p.complete(MSG)
    assert not e.value.retryable  # a miss is final: no retry loop, no live call


def test_replay_provider_holds_no_reference_to_any_transport():
    src = (SRC / "agents" / "providers" / "cassette.py").read_text(encoding="utf-8")
    cls = src[src.index("class ReplayProvider"):]
    for token in ("JsonTransport", ".post(", "RecordingProvider", "build_provider", "inner", "requests", "Anthropic", "OpenAI"):
        assert token not in cls, token  # replay can only read the cassette


# ------------------------------------------------------------------ secrets and generated artifacts
def test_no_secret_or_generated_artifact_is_tracked():
    files = _git("ls-files")
    if files is None:
        pytest.skip("not a git checkout")
    tracked = files.splitlines()
    bad = [f for f in tracked if re.search(r"(^|/)\.env$|\.venv/|__pycache__|\.pyc$|\.db(-wal|-shm)?$|\.xlsx$|\.jsonl$", f)]
    assert bad == []
    assert not [f for f in tracked if f.startswith(("data/", "logs/", ".cache/")) and not f.endswith(".gitkeep")]
    # "sk-test-..." is the explicit marker for the fake keys used by tests; real keys never start that way
    pat = re.compile(r"sk-(?!test-)[A-Za-z0-9_\-]{20,}|sk-ant-[A-Za-z0-9_\-]{10,}|ghp_[A-Za-z0-9]{20,}|github_pat_|AKIA[0-9A-Z]{16}|"
                     r"-----BEGIN (RSA |EC )?PRIVATE KEY|hf_[A-Za-z0-9]{20,}")
    leaks = []
    for f in tracked:
        if f == "tests/unit/test_m8_guards.py":  # this file necessarily contains the patterns it scans for
            continue
        path = ROOT / f
        if path.suffix in {".py", ".md", ".toml", ".example", ".txt", ".json", ".yml", ".yaml", ""} and path.is_file():
            try:
                if pat.search(path.read_text(encoding="utf-8")):
                    leaks.append(f)
            except UnicodeDecodeError:
                pass
    assert leaks == []


def test_gitignore_covers_secrets_databases_cassettes_and_caches():
    if _git("rev-parse", "--is-inside-work-tree") is None:
        pytest.skip("not a git checkout")
    for p in (".env", "data/run.db", "data/run.db-wal", "logs/app.log", ".cache/llm_cassettes/default.jsonl",
              ".cache/pages/a.txt", "out.xlsx", ".venv/x", "src/corpdisc/__pycache__/a.pyc"):
        assert _git("check-ignore", "-q", p) is not None or subprocess.run(
            ["git", "check-ignore", "-q", p], cwd=ROOT).returncode == 0, f"{p} is not ignored"
