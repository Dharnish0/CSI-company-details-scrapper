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
        description="Corporate hierarchy discovery. Phase 0 (M0): project foundation only.",
    )
    parser.add_argument("--version", action="version", version=f"corpdisc {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser("init-db", help="create and verify the SQLite database at DB_PATH")
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "init-db":
        return _cmd_init_db()
    parser.print_help()
    return 0
