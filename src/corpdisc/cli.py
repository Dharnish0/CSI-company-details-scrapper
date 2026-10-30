"""Command line interface: init-db, llms, run, resume, report, export, verify."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from corpdisc import __version__
from corpdisc.config import PROVIDER_MODEL_FIELDS, load_settings
from corpdisc.db.connection import DatabaseError, init_db
from corpdisc.llm_registry import (
    build_registry,
    discover_ollama,
    format_registry,
    ollama_base_url,
    ollama_model_installed,
)
from corpdisc.logging_setup import get_logger, setup_logging
from corpdisc.models.domain import run_status_label

# Providers that cost money per call and therefore need an explicit confirmation (--yes or an interactive "y").
BILLABLE_PROVIDERS = frozenset({"openai", "anthropic", "gemini", "groq"})

# Exit codes of `run` / `resume`.
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_INCOMPLETE, EXIT_PARTIAL = 0, 1, 2, 3, 4
RUN_EXIT_CODES = {"COMPLETED": EXIT_OK, "PARTIAL": EXIT_PARTIAL, "FAILED": EXIT_FAILED}  # all others: EXIT_INCOMPLETE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Corporate hierarchy discovery: init-db, llms, run, resume, report, export and verify.",
    )
    parser.add_argument("--version", action="version", version=f"corpdisc {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser("init-db", help="create and verify the SQLite database at DB_PATH")
    sub.add_parser("llms", help="list LLM providers and models (Ollama: installed models only)")
    for name, text in (("run", "start a new research run from START_URL / ROOT_COMPANY"),
                       ("resume", "continue the latest (or --run-id) run from the queue state")):
        sp = sub.add_parser(name, help=text)
        sp.add_argument("--max-tasks", type=int, default=None, help="stop after N tasks (the run stays resumable)")
        sp.add_argument("--yes", action="store_true", help="confirm that a real (billable) LLM provider may be used")
        sp.add_argument("--provider", type=str, default=None, help="LLM provider (e.g., groq, gemini, ollama)")
        sp.add_argument("--model", type=str, default=None, help="LLM model name (e.g., openai/gpt-oss-120b)")
        if name == "resume":
            sp.add_argument("--run-id", type=int, default=None)
    ep = sub.add_parser("export", help="assign Tree IDs and write the Excel workbook from the database")
    ep.add_argument("--run-id", type=int, default=None)
    ep.add_argument("--output", type=str, default=None, help="workbook path (default OUTPUT_XLSX)")
    vp = sub.add_parser("verify", help="re-check data quality from the database (evidence grounding, duplicates, cycles ...)")
    vp.add_argument("--run-id", type=int, default=None)
    rp = sub.add_parser("report", help="print the calibration report (status, calls, failures, tokens, cost, review ratio)")
    rp.add_argument("--run-id", type=int, default=None)
    rp.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def _format_config_error(exc: ValidationError) -> str:
    lines = ["Invalid configuration:"]
    for err in exc.errors(include_input=False):
        field = ".".join(str(p) for p in err["loc"]).upper() or "settings"
        lines.append(f"  {field}: {err['msg']}")
    return "\n".join(lines)


def _load(setup: bool = True):  # type: ignore[no-untyped-def]
    try:
        settings = load_settings()
    except ValidationError as exc:
        print(_format_config_error(exc), file=sys.stderr)
        return None
    if setup:
        setup_logging(settings)
    return settings


def _cmd_llms() -> int:
    """List providers and models. Ollama models appear only when the server reports them installed."""
    settings = _load()
    if settings is None:
        return EXIT_USAGE
    print(format_registry(build_registry(settings), show_unavailable=True))
    return EXIT_OK


def _cmd_init_db() -> int:
    settings = _load(setup=False)
    if settings is None:
        return EXIT_USAGE
    setup_logging(settings)
    log = get_logger("corpdisc.cli")
    try:
        report = init_db(settings.db_path)
    except DatabaseError as exc:
        log.error("Database initialization failed: %s", exc)
        print(f"Database initialization failed: {exc}", file=sys.stderr)
        return EXIT_FAILED
    message = (
        f"Database initialized at {report.db_path}: {report.table_count} tables, "
        f"foreign_keys={'ON' if report.foreign_keys else 'OFF'}, "
        f"journal_mode={report.journal_mode}"
    )
    log.info(message)
    print(message)
    return EXIT_OK


# ---------------------------------------------------------------------------------------------------- run / resume
def _stdin_is_interactive() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (ValueError, OSError):
        return False


def _choose_interactively(settings) -> tuple[str, str] | None:  # type: ignore[no-untyped-def]
    """Menu over the models that are configured AND available. Returns (provider, model) or None when cancelled."""
    registry = build_registry(settings)
    print(format_registry(registry, show_unavailable=False))
    items = registry.selectable()
    if not items:
        print("No LLM providers are configured and available.", file=sys.stderr)
        return None
    try:
        idx = int(input(f"Select LLM [1-{len(items)}]: ").strip())
    except (ValueError, EOFError, KeyboardInterrupt):
        print("Selection cancelled.", file=sys.stderr)
        return None
    chosen = registry.get_by_index(idx)
    if chosen is None:
        print("Selection cancelled.", file=sys.stderr)
        return None
    return chosen.provider_id, chosen.model_name


def _resolve_selection(args: argparse.Namespace, settings) -> tuple[str | None, str | None] | None:  # type: ignore[no-untyped-def]
    """Provider/model for this run: CLI flags, else a menu on a terminal, else the configured settings."""
    provider = (args.provider or "").strip().lower() or None
    model = (args.model or "").strip() or None
    if provider is None and model is None and _stdin_is_interactive():
        return _choose_interactively(settings)
    provider = provider or (settings.run_provider or settings.llm_provider or "").strip().lower() or None
    if provider and model is None:
        model = (settings.run_model or getattr(settings, PROVIDER_MODEL_FIELDS.get(provider, "llm_model")) or "").strip() or None
    return provider, model


def _check_ollama(settings, model: str | None) -> str | None:  # type: ignore[no-untyped-def]
    """Return an error message when the requested Ollama model is not installed (or the server is down)."""
    probe = discover_ollama(ollama_base_url(settings))
    if not probe.reachable:
        return f"Ollama server not reachable at {probe.base_url} ({probe.error}). Start it with: ollama serve"
    if not model:
        return "no Ollama model selected (use --model or LLM_MODEL); installed: " + (", ".join(probe.models) or "none")
    if not ollama_model_installed(probe, model):
        return (f"Ollama model '{model}' is not installed (installed: {', '.join(probe.models) or 'none'}). "
                f"Install it with: ollama pull {model}")
    return None


def _print_summary(rt, summary) -> None:  # type: ignore[no-untyped-def]
    label = run_status_label(summary.status)
    d = summary.queue_detail or {}
    print(f"Run {rt.run_id}: {label} ({summary.stopped_reason}); {summary.processed} tasks, "
          f"outcomes={dict(summary.outcomes)}, failed={summary.failed_tasks}, retries={summary.retries}")
    if d:
        print(f"Queue: failed={d.get('failed', 0)} done_with_gaps={d.get('partial', 0)} waiting_retry={d.get('waiting', 0)} "
          f"retryable_gaps={d.get('retryable_partial', 0)}")
    fin = summary.finalize
    if fin is not None:
        print(f"Finalize: {fin.outcome}; confirmed={fin.confirmed} review={fin.review_required} merges={fin.merges} "
              f"demotions={fin.demotions}+{fin.cascade_demotions}")
    hints = {
        "PAUSED": ("Waiting for retryable work to become due. Resume later with: python main.py resume"
                   if summary.stopped_reason == "RETRY_WAITING" else
                   "The LLM is unavailable or not configured (circuit breaker open). Fix it, then: python main.py resume"),
        "PARTIAL": "Finished with gaps: some verification/extraction failed. Re-run with: python main.py resume",
        "FAILED": "No task completed successfully. See: python main.py report",
        "RUNNING": "Stopped early; the run is resumable: python main.py resume",
        "BUDGET_EXHAUSTED": "A budget limit was reached. Raise the limit or resume later: python main.py resume",
    }
    if summary.status in hints:
        print(hints[summary.status])
    print("Details: python main.py report")


def _cmd_run(args: argparse.Namespace, *, resume: bool) -> int:
    from corpdisc.app import build_runtime, latest_run_id
    from corpdisc.db.connection import connect_readonly

    settings = _load()
    if settings is None:
        return EXIT_USAGE
    interactive = not (args.provider or args.model) and _stdin_is_interactive()
    selection = _resolve_selection(args, settings)
    if selection is None:
        return EXIT_USAGE
    provider, model = selection

    if provider == "gemini" and not settings.gemini_api_key:
        print("ERROR: GEMINI_API_KEY is not configured.", file=sys.stderr)
        return EXIT_USAGE
    if provider == "groq" and not settings.groq_api_key:
        print("ERROR: GROQ_API_KEY is not configured.", file=sys.stderr)
        return EXIT_USAGE
    if provider == "ollama" and (problem := _check_ollama(settings, model)):
        print(f"ERROR: {problem}", file=sys.stderr)
        return EXIT_USAGE

    if provider:
        print(f"Selected provider: {provider}")
        print(f"Selected model: {model or '(not set)'}")
    if provider in BILLABLE_PROVIDERS and not args.yes:
        notice = (f"billable provider '{provider}' (model {model or '?'}) against {settings.start_url}. "
                  f"Limits: max_depth={settings.max_depth}, max_total_pages={settings.max_total_pages}, "
                  f"token budget={settings.llm_token_budget or 'unlimited'}.")
        if not interactive:
            print(f"Refusing to use the {notice}\nRe-run with --yes to confirm; refusing without --yes.", file=sys.stderr)
            return EXIT_USAGE
        print(f"Running with {notice}")
        try:
            if input("Continue? [y/N]: ").strip().lower() != "y":
                print("Run cancelled.")
                return EXIT_OK
        except (EOFError, KeyboardInterrupt):
            print("\nRun cancelled.")
            return EXIT_OK

    run_id = None
    if resume:
        init_db(settings.db_path)
        probe = connect_readonly(settings.db_path)
        run_id = args.run_id or latest_run_id(probe)
        probe.close()
        if run_id is None:
            print("No run to resume. Start one with: python main.py run", file=sys.stderr)
            return EXIT_USAGE

    try:
        rt = build_runtime(settings, run_id=run_id, run_provider=provider, run_model=model)
    except (ValueError, DatabaseError) as exc:
        print(f"Cannot start: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        rt.supervisor.seed_root()
        summary = rt.supervisor.resume(max_tasks=args.max_tasks) if resume else rt.supervisor.run(max_tasks=args.max_tasks)
        _print_summary(rt, summary)
        return RUN_EXIT_CODES.get(summary.status, EXIT_INCOMPLETE)
    finally:
        rt.close()


# ---------------------------------------------------------------------------------------- report / export / verify
def _stack(settings, run_id: int, *, write: bool):  # type: ignore[no-untyped-def]
    """Database-only services (no HTTP client, no LLM): enough to number the tree and to re-verify evidence."""
    from corpdisc.clock import SystemClock
    from corpdisc.db.connection import connect_readonly, connect_writer
    from corpdisc.db.repos.pages import PageRepo
    from corpdisc.services.committer import Committer
    from corpdisc.services.fetch import make_page_text_loader
    from corpdisc.services.pagecache import PageCache

    conn = connect_writer(settings.db_path) if write else connect_readonly(settings.db_path)
    clock = SystemClock()
    loader = make_page_text_loader(PageRepo(conn, clock), PageCache(settings.cache_dir))
    return conn, loader, Committer.from_settings(conn, run_id=run_id, page_text=loader, settings=settings, clock=clock)


def _resolve_run(settings, run_id):  # type: ignore[no-untyped-def]
    from corpdisc.app import latest_run_id
    from corpdisc.db.connection import connect_readonly

    c = connect_readonly(settings.db_path)
    try:
        return run_id or latest_run_id(c)
    finally:
        c.close()


def _cmd_report(args: argparse.Namespace) -> int:
    from corpdisc.app import latest_run_id
    from corpdisc.db.connection import connect_readonly
    from corpdisc.services.calibration import build_report, format_report

    settings = _load(setup=False)
    if settings is None:
        return EXIT_USAGE
    conn = connect_readonly(settings.db_path)
    try:
        run_id = args.run_id or latest_run_id(conn)
        if run_id is None:
            print("No runs in the database.", file=sys.stderr)
            return EXIT_USAGE
        rep = build_report(conn, run_id)
    except (LookupError, DatabaseError) as exc:
        print(f"Cannot report: {exc}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        conn.close()
    print(json.dumps(rep, indent=2, sort_keys=True) if args.json else format_report(rep))
    return EXIT_OK


def _cmd_export(args: argparse.Namespace) -> int:
    from corpdisc.services.excel import export_workbook
    from corpdisc.services.tree import assign_tree_ids

    settings = _load(setup=False)
    if settings is None:
        return EXIT_USAGE
    rid = _resolve_run(settings, args.run_id)
    if rid is None:
        print("No runs in the database.", file=sys.stderr)
        return EXIT_USAGE
    conn, _, committer = _stack(settings, rid, write=True)
    try:
        root_code = conn.execute("SELECT root_code FROM runs WHERE run_id=?", (rid,)).fetchone()[0] or settings.root_code
        rows = assign_tree_ids(conn, committer, rid, root_code)
        summ = export_workbook(conn, rid, args.output or settings.output_xlsx)
    except (LookupError, RuntimeError, DatabaseError) as exc:
        print(f"Cannot export: {exc}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        conn.close()
    print(f"Run {rid}: numbered {len(rows)} confirmed entities; wrote {summ.path}")
    for sheet, n in summ.sheets.items():
        print(f"  {sheet:<18} {n} rows")
    return EXIT_OK


def _cmd_verify(args: argparse.Namespace) -> int:
    """Schema/pragma integrity plus the data-quality invariants (evidence grounding, cycles, duplicates ...)."""
    from corpdisc.db.connection import verify_db
    from corpdisc.services.cycle_guard import CycleGuard
    from corpdisc.services.data_quality import check_run, format_checks

    settings = _load(setup=False)
    if settings is None:
        return EXIT_USAGE
    rid = _resolve_run(settings, args.run_id)
    if rid is None:
        print("No runs in the database.", file=sys.stderr)
        return EXIT_USAGE
    try:
        report = verify_db(settings.db_path)
    except DatabaseError as exc:
        print(f"Database verification failed: {exc}", file=sys.stderr)
        return EXIT_FAILED
    conn, loader, committer = _stack(settings, rid, write=False)
    try:
        checks = check_run(conn, rid, committer=committer, cycle=CycleGuard(conn), page_text=loader,
                           shared_domains=frozenset(d.lower() for d in settings.shared_platform_domains))
    finally:
        conn.close()
    print(f"Data-quality verification of run {rid}:")
    print(f"  database: {report.table_count} tables, foreign_keys={'ON' if report.foreign_keys else 'OFF'}, "
          f"journal_mode={report.journal_mode}, integrity ok")
    print(format_checks(checks))
    failed = [c for c in checks if c.status == "FAIL"]
    if failed:
        print(f"{len(failed)} check(s) FAILED.")
    return EXIT_FAILED if failed else EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "init-db":
        return _cmd_init_db()
    if args.command == "llms":
        return _cmd_llms()
    if args.command in ("run", "resume"):
        return _cmd_run(args, resume=args.command == "resume")
    if args.command == "report":
        return _cmd_report(args)
    if args.command == "export":
        return _cmd_export(args)
    if args.command == "verify":
        return _cmd_verify(args)
    parser.print_help()
    return EXIT_OK
