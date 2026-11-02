"""``--slow-crawl`` on run/resume: parser, settings override, billable notice, exit-code constants."""
from __future__ import annotations

import pytest

import corpdisc.app as app
import corpdisc.cli as cli
from corpdisc.cli import build_parser, main
from tests.test_cli_status import env, fake_runtime  # noqa: F401 - shared fixtures


@pytest.mark.parametrize("argv", [["run"], ["resume"], ["resume", "--run-id", "3"]])
def test_flag_is_accepted_on_run_and_resume(argv):
    p = build_parser()
    assert p.parse_args([*argv, "--slow-crawl"]).slow_crawl is True
    assert p.parse_args(argv).slow_crawl is False


def test_flag_is_rejected_on_other_commands(capsys):
    for cmd in ("report", "export", "verify", "init-db", "llms"):
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args([cmd, "--slow-crawl"])
        assert exc.value.code == 2
    capsys.readouterr()


def test_run_help_lists_the_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["run", "--help"])
    assert exc.value.code == 0
    assert "--slow-crawl" in capsys.readouterr().out


def _capture(monkeypatch):
    seen = {}

    def rt(settings, **kw):
        seen["settings"], seen["kw"] = settings, kw
        return fake_runtime("COMPLETED")

    monkeypatch.setattr(app, "build_runtime", rt)
    return seen


def test_absent_flag_leaves_slow_crawl_off(env, monkeypatch):  # noqa: F811
    seen = _capture(monkeypatch)
    assert main(["run", "--provider", "fake", "--model", "m"]) == 0
    assert seen["settings"].slow_crawl is False


def test_flag_turns_slow_crawl_on_for_run_and_resume(env, monkeypatch):  # noqa: F811
    seen = _capture(monkeypatch)
    assert main(["run", "--provider", "fake", "--model", "m", "--slow-crawl"]) == 0
    assert seen["settings"].slow_crawl is True
    seen.clear()
    assert main(["resume", "--run-id", "1", "--provider", "fake", "--model", "m", "--slow-crawl"]) == 0
    assert seen["settings"].slow_crawl is True


def test_environment_value_enables_slow_crawl(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SLOW_CRAWL", "true")
    seen = _capture(monkeypatch)
    assert main(["run", "--provider", "fake", "--model", "m"]) == 0
    assert seen["settings"].slow_crawl is True


def test_billable_notice_mentions_slow_crawl(env, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setattr(app, "build_runtime", lambda *a, **k: pytest.fail("must not start without --yes"))
    assert main(["run", "--provider", "groq", "--model", "m", "--slow-crawl"]) == 2
    err = capsys.readouterr().err
    assert "Slow crawl" in err and "24 h" in err and "250000" in err
    assert main(["run", "--provider", "groq", "--model", "m"]) == 2
    assert "Slow crawl" not in capsys.readouterr().err


def test_exit_codes_are_extended_without_changing_existing_ones():
    assert (cli.EXIT_OK, cli.EXIT_FAILED, cli.EXIT_USAGE, cli.EXIT_INCOMPLETE, cli.EXIT_PARTIAL) == (0, 1, 2, 3, 4)
    assert (cli.EXIT_INTERRUPTED, cli.EXIT_LOCKED) == (5, 6)
