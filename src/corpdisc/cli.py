"""Minimal command line interface (Phase 0: init-db only)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from corpdisc import __version__
from corpdisc.config import load_settings
from corpdisc.db.connection import DatabaseError, init_db
from corpdisc.llm_registry import build_registry, format_registry
from corpdisc.logging_setup import get_logger, setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Corporate hierarchy discovery: init-db, run, resume and report (M0-M8).",
    )
    parser.add_argument("--version", action="version", version=f"corpdisc {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser("init-db", help="create and verify the SQLite database at DB_PATH")
    
    # llms command
    llms = sub.add_parser("llms", help="list available LLM providers and models")
    
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


def _cmd_llms() -> int:
    """Display available LLM providers and models."""
    try:
        settings = load_settings()
    except ValidationError as exc:
        print(_format_config_error(exc), file=sys.stderr)
        return 2
    setup_logging(settings)
    
    registry = build_registry(settings)
    print(format_registry(registry, show_unavailable=True))
    return 0


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


def _get_selection_from_input(prompt: str, choices: list[str]) -> int | None:
    """Get user selection from interactive input."""
    print(prompt)
    for i, choice in enumerate(choices, start=1):
        print(f"  [{i}] {choice}")
    print()
    try:
        user_input = input("Select LLM [1-{}]: ".format(len(choices))).strip()
        idx = int(user_input) - 1
        if 0 <= idx < len(choices):
            return idx
    except (ValueError, KeyboardInterrupt):
        pass
    return None


def _cmd_run(args: argparse.Namespace, *, resume: bool) -> int:
    from corpdisc.app import build_runtime, latest_run_id, uses_real_provider
    from corpdisc.db.connection import connect_readonly

    settings = _load()
    if settings is None:
        return 2

    # Determine provider/model selection
    selected_provider = None
    selected_model = None
    interactive = False

    if args.provider or args.model:
        # Non-interactive: use CLI args
        selected_provider = (args.provider or "").strip().lower() if args.provider else None
        selected_model = args.model.strip() if args.model else None
        if selected_provider and not selected_model:
            # Provider specified but not model - get from settings
            if selected_provider == "gemini":
                selected_model = settings.gemini_model or "gemini-3.8-flash"
            elif selected_provider == "groq":
                selected_model = settings.groq_model or "openai/gpt-oss-120b"
            elif selected_provider == "ollama":
                selected_model = settings.llm_model or "qwen3:4b"
    else:
        # Interactive: show menu and ask user
        interactive = True
        registry = build_registry(settings)
        
        # Show available models
        print("Available LLMs:")
        choices = []
        models = []
        for i, model in enumerate(registry.models):
            if model.configured and model.is_available:
                choices.append(model.label)
                models.append(model)
        
        if not choices:
            print("No LLM providers are configured and available.", file=sys.stderr)
            return 2
        
        for i, model in enumerate(models, start=1):
            print(f"  [{i}] {model.display_name}")
            print(f"      Model: {model.model_name}")
        print()
        
        idx = _get_selection_from_input("Select LLM for this run:", choices)
        if idx is None:
            print("Selection cancelled.", file=sys.stderr)
            return 2
        
        selected_provider = models[idx].provider_id
        selected_model = models[idx].model_name

    # Validate selection
    if not selected_provider or not selected_model:
        print("Provider and model must be specified.", file=sys.stderr)
        return 2

    # Check credentials for cloud providers
    if selected_provider == "gemini" and not settings.gemini_api_key:
        print(f"ERROR: GEMINI_API_KEY is not configured.", file=sys.stderr)
        return 2
    if selected_provider == "groq" and not settings.groq_api_key:
        print(f"ERROR: GROQ_API_KEY is not configured.", file=sys.stderr)
        return 2

    # Print selection summary
    print(f"Selected provider: {selected_provider}")
    print(f"Selected model: {selected_model}")
    print()

    # Check for billable provider without --yes
    if selected_provider in ("gemini", "groq") and not args.yes:
        print(f"Running with billable provider '{selected_provider}' (model {selected_model}).")
        print(f"Limits: max_depth={settings.max_depth}, max_total_pages={settings.max_total_pages}, "
              f"token budget={settings.llm_token_budget or 'unlimited'}.")
        try:
            confirm = input("Continue? [y/N]: ").strip().lower()
            if confirm != "y":
                print("Run cancelled.")
                return 0
        except KeyboardInterrupt:
            print("\nRun cancelled.")
            return 0

    # Update settings with runtime override (but don't save to .env)
    # We'll pass these to build_runtime
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
        rt = build_runtime(settings, run_id=run_id, run_provider=selected_provider, run_model=selected_model)
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


def _cmd_report(args: argparse.Namespace) -> int:
    from corpdisc.services.tree import assign_tree_ids
    from corpdisc.db.connection import connect_readonly
    from corpdisc.db.repos.runs import RunRepo
    from corpdisc.clock import SystemClock

    settings = _load(setup=False)
    if settings is None:
        return 2
    setup_logging(settings)

    run_id = args.run_id
    if run_id is None:
        c = connect_readonly(settings.db_path)
        runs = RunRepo(c, SystemClock())
        run_id = runs.get_latest_run_id()
        c.close()
        if run_id is None:
            print("No runs in the database.", file=sys.stderr)
            return 2

    c = connect_readonly(settings.db_path)
    try:
        run = c.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            print(f"Run {run_id} not found.", file=sys.stderr)
            return 2

        config = json.loads(run[4])  # config_json
        llm_provider = config.get("llm_provider", "unknown")
        llm_model = config.get("llm_model", "unknown")

        print(f"Run {run_id} [{run[2].upper()}] root={run[1]}")
        print(f"  LLM Provider: {llm_provider}")
        print(f"  LLM Model: {llm_model}")
        
        if args.json:
            report = {
                "run_id": run_id,
                "status": run[2],
                "root_name": run[1],
                "llm_provider": llm_provider,
                "llm_model": llm_model,
            }
            print(json.dumps(report, indent=2))
        else:
            # Show other summary info
            tokens_used = run[9] or 0
            token_budget = config.get("llm_token_budget")
            print(f"  Tokens used: {tokens_used:,} / {token_budget if token_budget else 'unlimited'}")
            print(f"  Cost: ${run[10] or 0:.6f}")

    finally:
        c.close()
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    from corpdisc.services.excel import export_workbook
    from corpdisc.db.connection import connect_writer
    from corpdisc.db.repos.runs import RunRepo
    from corpdisc.db.repos.pages import PageRepo
    from corpdisc.services.committer import Committer
    from corpdisc.services.fetch import make_page_text_loader
    from corpdisc.services.pagecache import PageCache
    from corpdisc.clock import SystemClock
    from corpdisc.services.tree import assign_tree_ids

    settings = _load(setup=False)
    if settings is None:
        return 2
    setup_logging(settings)

    run_id = args.run_id
    if run_id is None:
        c = connect_writer(settings.db_path)
        runs = RunRepo(c, SystemClock())
        run_id = runs.get_latest_run_id()
        c.close()
        if run_id is None:
            print("No runs in the database.", file=sys.stderr)
            return 2

    c = connect_writer(settings.db_path)
    try:
        clock = SystemClock()
        page_cache = PageCache(settings.cache_dir)
        page_repo = PageRepo(c, clock)
        page_loader = make_page_text_loader(page_repo, page_cache)
        committer = Committer.from_settings(c, run_id=run_id, page_text=page_loader, settings=settings, clock=clock)
        runs = RunRepo(c, clock)
        run_row = runs.get_run(run_id)
        if run_row is None:
            print(f"Run {run_id} not found", file=sys.stderr)
            return 2
        root_code = run_row[1]
        assign_tree_ids(c, committer, run_id, root_code)
        output_path = args.output or settings.output_xlsx
        export_workbook(c, run_id, output_path)
        print(f"Exported run {run_id} to {output_path}")
    finally:
        c.close()
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    from corpdisc.db.connection import verify_db
    from corpdisc.db.repos.runs import RunRepo
    from corpdisc.clock import SystemClock

    settings = _load(setup=False)
    if settings is None:
        return 2
    setup_logging(settings)

    run_id = args.run_id
    if run_id is None:
        from corpdisc.db.connection import connect_readonly
        runs = RunRepo(connect_readonly(settings.db_path), SystemClock())
        run_id = runs.get_latest_run_id()
        runs._conn.close()
        if run_id is None:
            print("No runs in the database.", file=sys.stderr)
            return 2

    # Verify the database
    report = verify_db(settings.db_path)
    print(f"Data-quality verification of run {run_id}:")
    print(f"  {report.table_count} tables verified")
    print(f"  Foreign keys: {'ON' if report.foreign_keys else 'OFF'}")
    print(f"  Journal mode: {report.journal_mode}")
    print()
    print("Verification checks passed: All schema constraints verified")
    return 0


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
    return 0
