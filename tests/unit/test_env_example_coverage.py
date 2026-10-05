"""Every Settings field must be documented in .env.example (a missing entry means users cannot discover the knob)."""
from __future__ import annotations

import re
from pathlib import Path

from corpdisc.config import Settings

ENV = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")


def test_every_setting_is_listed_once_in_env_example():
    listed = re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", ENV, re.M)
    missing = sorted(f.upper() for f in Settings.model_fields if f.upper() not in listed)
    assert missing == [], f"settings missing from .env.example: {missing}"
    dupes = sorted({k for k in listed if listed.count(k) > 1})
    assert dupes == [], f"duplicated in .env.example: {dupes}"


def test_env_example_lists_only_real_settings():
    listed = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", ENV, re.M))
    unknown = sorted(listed - {f.upper() for f in Settings.model_fields})
    assert unknown == [], f"not a Settings field: {unknown}"


def test_secret_entries_are_blank():
    for key in ("LLM_API_KEY", "SEARCH_API_KEY"):
        assert re.search(rf"^{key}=\s*$", ENV, re.M)
