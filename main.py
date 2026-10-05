"""Thin entry point: python main.py <command>."""

import sys
from pathlib import Path

try:
    import corpdisc  # noqa: F401
except ModuleNotFoundError:  # package not installed: fall back to the src/ layout
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from corpdisc.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
