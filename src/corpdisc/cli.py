"""Minimal command line interface (Phase 0: init-db only)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from corpdisc import __version__
from corpdisc.config import load_settings
from corpdisc.db.connection import DatabaseError, init_db
from corpdisc.logging_setup import get_logger, setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Corporate hierarchy discovery: init-db, run, resume and report (M0-M8).",
    )
    parser.add_argument("--version", action="version", version=f"corpdisc {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser("init-db", help="create and verify the SQLite database at DB_PATH")
    for name, text in (("run", "start a new research run from START_URL / ROOT_COMPANY"),
                       ("resume", "continue the latest (or --run-id) run from the queue state")):
        sp = sub.add_parser(name, help=text)
        sp.add_argument("--max-tasks", type=int, default=None, help="stop after N tasks (the run stays resumable)")
        sp.add_argument("--yes", action="store_true", help="confirm that a real (billable) LLM provider may be used")
        if name == "resume":
            sp.add_argument("--run-id", type=int, default=None)
    ep = sub.add_parser("export", help="assign Tree IDs and write the Excel workbook from the database")
    ep.add_argument("--run-id", type=int, default=None)
    ep.add_argument("--output", type=str, default=None, help="workbook path (default OUTPUT_XLSX)")
    vp = sub.add_parser("verify", help="re-check data quality from the database (evidence grounding, duplicates, cycles ...)")
    vp.add_argument("--run-id", type=int, default=None)
    rp = sub.add_parser("report", help="print the calibration report (calls, cost, cache rate, review ratio)")
    rp.add_argument("--run-id", type=int, default=None)
    rp.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def _format_config_error(exc: ValidationError) -> str:
    lines = ["Invalid configuration:"]
    for err in exc.errors(include_input=False):
        field = ".".join(str(p) for p in err["loc"]).upper() or "settings"
        lines.append(f"  {field}: {err['msg']}")
    return "\n".join(lines)


def _cmd_init_db() -> int:
    try:
        settings = load_settings()
    except ValidationError as exc:
        print(_format_config_error(exc), file=sys.stderr)
        return 2
    setup_logging(settings)
    log = get_logger("corpdisc.cli")
    try:
        report = init_db(settings.db_path)
    except DatabaseError as exc:
        log.error("Database initialization failed: %s", exc)
        print(f"Database initialization failed: {exc}", file=sys.stderr)
        return 1
    message = (
        f"Database initialized at {report.db_path}: {report.table_count} tables, "
        f"foreign_keys={'ON' if report.foreign_keys else 'OFF'}, "
        f"journal_mode={report.journal_mode}"
    )
    log.info(message)
    print(message)
    return 0


def _load(setup: bool = True):  # type: ignore[no-untyped-def]
    try:
        settings = load_settings()
    except ValidationError as exc:
        print(_format_config_error(exc), file=sys.stderr)
        return None
    if setup:
        setup_logging(settings)
    return settings


def _cmd_run(args: argparse.Namespace, *, resume: bool) -> int:
    from corpdisc.app import build_runtime, latest_run_id, uses_real_provider
    from corpdisc.db.connection import connect_readonly

    settings = _load()
    if settings is None:
        return 2
    if uses_real_provider(settings) and not args.yes:
        print(f"Refusing to use the billable provider '{settings.llm_provider}' (model {settings.llm_model or '?'}) "
              f"against {settings.start_url} without --yes.\nLimits: max_depth={settings.max_depth}, "
              f"max_total_pages={settings.max_total_pages}, token budget={settings.llm_token_budget or 'unlimited'}.",
              file=sys.stderr)
        return 2
    run_id = None
    if resume:
        init_db(settings.db_path)
        probe = connect_readonly(settings.db_path)
        run_id = args.run_id or latest_run_id(probe)
        probe.close()
        if run_id is None:
            print("No run to resume. Start one with: python main.py run", file=sys.stderr)
            return 2
    try:
        rt = build_runtime(settings, run_id=run_id)
    except (ValueError, DatabaseError) as exc:
        print(f"Cannot start: {exc}", file=sys.stderr)
        return 2
    try:
        rt.supervisor.seed_root()
        summary = rt.supervisor.resume(max_tasks=args.max_tasks) if resume else rt.supervisor.run(max_tasks=args.max_tasks)
        fin = summary.finalize
        print(f"Run {rt.run_id}: {summary.status} ({summary.stopped_reason}); {summary.processed} tasks, "
              f"outcomes={dict(summary.outcomes)}, failed={summary.failed_tasks}")
        if fin is not None:
            print(f"Finalize: {fin.outcome}; confirmed={fin.confirmed} review={fin.review_required} merges={fin.merges} "
                  f"demotions={fin.demotions}+{fin.cascade_demotions}")
        if summary.status == "PAUSED":
            print("The LLM is unavailable or not configured (circuit breaker open). Fix it, then: python main.py resume")
        print("Details: python main.py report")
        return 0 if summary.status == "COMPLETED" else 3
    finally:
        rt.close()


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


def _cmd_export(args: argparse.Namespace) -> int:
    from corpdisc.services.excel import export_workbook
    from corpdisc.services.tree import assign_tree_ids

    settings = _load(setup=False)
    if settings is None:
        return 2
    rid = _resolve_run(settings, args.run_id)
    if rid is None:
        print("No runs in the database.", file=sys.stderr)
        return 2
    conn, _, committer = _stack(settings, rid, write=True)
    try:
        root_code = conn.execute("SELECT root_code FROM runs WHERE run_id=?", (rid,)).fetchone()[0] or settings.root_code
        rows = assign_tree_ids(conn, committer, rid, root_code)
        summ = export_workbook(conn, rid, args.output or settings.output_xlsx)
    except (LookupError, RuntimeError, DatabaseError) as exc:
        print(f"Cannot export: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(f"Run {rid}: numbered {len(rows)} confirmed entities; wrote {summ.path}")
    for sheet, n in summ.sheets.items():
        print(f"  {sheet:<18} {n} rows")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    from corpdisc.services.cycle_guard import CycleGuard
    from corpdisc.services.data_quality import check_run, format_checks

    settings = _load(setup=False)
    if settings is None:
        return 2
    rid = _resolve_run(settings, args.run_id)
    if rid is None:
        print("No runs in the database.", file=sys.stderr)
        return 2
    conn, loader, committer = _stack(settings, rid, write=False)
    try:
        checks = check_run(conn, rid, committer=committer, cycle=CycleGuard(conn), page_text=loader,
                           shared_domains=frozenset(d.lower() for d in settings.shared_platform_domains))
    finally:
        conn.close()
    print(f"Data-quality verification of run {rid}:")
    print(format_checks(checks))
    return 1 if any(c.status == "FAIL" for c in checks) else 0


def _cmd_report(args: argparse.Namespace) -> int:
    import json

    from corpdisc.app import latest_run_id
    from corpdisc.db.connection import connect_readonly
    from corpdisc.services.calibration import build_report, format_report

    settings = _load(setup=False)
    if settings is None:
        return 2
    conn = connect_readonly(settings.db_path)
    try:
        run_id = args.run_id or latest_run_id(conn)
        if run_id is None:
            print("No runs in the database.", file=sys.stderr)
            return 2
        rep = build_report(conn, run_id)
    except (LookupError, DatabaseError) as exc:
        print(f"Cannot report: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(json.dumps(rep, indent=2, sort_keys=True) if args.json else format_report(rep))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "init-db":
        return _cmd_init_db()
    if args.command in ("run", "resume"):
        return _cmd_run(args, resume=args.command == "resume")
    if args.command == "report":
        return _cmd_report(args)
    if args.command == "export":
        return _cmd_export(args)
    if args.command == "verify":
        return _cmd_verify(args)
    parser.print_help()
    return 0
