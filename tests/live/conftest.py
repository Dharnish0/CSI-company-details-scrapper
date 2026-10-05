"""Live (billable / networked) tests. Everything here is opt-in and double guarded:

1. ``addopts = -m 'not live and not live_site'`` deselects these tests in every ordinary run.
2. They skip themselves unless CORPDISC_LIVE=1 and a key is present (CORPDISC_LIVE_SITE=1 as well for the real site).

Run:  CORPDISC_LIVE=1 CORPDISC_LIVE_PROVIDER=anthropic CORPDISC_LIVE_API_KEY=... CORPDISC_LIVE_MODEL=... \
      pytest tests/live -m live -s
The key is read only from the environment, passed in memory to the adapter, and never written anywhere.
"""
from __future__ import annotations

import os

import pytest

# The parent conftest blocks the network and strips configuration variables for ordinary tests. Live tests must
# opt out of exactly those two autouse fixtures (same names override the parent's).


@pytest.fixture(autouse=True)
def _block_network():
    yield


@pytest.fixture(autouse=True)
def _hermetic_env():
    yield


@pytest.fixture(scope="session")
def live_cfg():
    if os.environ.get("CORPDISC_LIVE") != "1":
        pytest.skip("set CORPDISC_LIVE=1 to run live tests")
    cfg = {k: os.environ.get(f"CORPDISC_LIVE_{k.upper()}", "") for k in ("provider", "api_key", "model", "base_url")}
    missing = [k for k in ("provider", "api_key", "model") if not cfg[k]]
    if missing or cfg["provider"] not in ("anthropic", "openai"):
        pytest.skip(f"live config incomplete or unsupported: {missing or cfg['provider']!r}")
    cfg["token_budget"] = int(os.environ.get("CORPDISC_LIVE_TOKEN_BUDGET", "400000"))
    cfg["price_in"] = float(os.environ.get("CORPDISC_LIVE_PRICE_IN", "0"))
    cfg["price_out"] = float(os.environ.get("CORPDISC_LIVE_PRICE_OUT", "0"))
    return cfg
