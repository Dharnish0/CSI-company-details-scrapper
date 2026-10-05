"""Hermetic test setup: no real .env, no config env vars, loopback-only network, clean logging."""
from __future__ import annotations
import socket
from pathlib import Path
import pytest
from corpdisc.config import Settings
from corpdisc.logging_setup import shutdown_logging
from tests.helpers.http_server import LocalSite

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_ENV_VARS = tuple(name.upper() for name in Settings.model_fields)
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for name in CONFIG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # Relative paths and any accidental ".env" lookup resolve inside tmp_path.
    monkeypatch.chdir(tmp_path)
    yield
    shutdown_logging()  # release log file handles so Windows can delete tmp files


def _host_of(address: object) -> str | None:
    if isinstance(address, tuple) and address:
        return str(address[0])
    return None


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch):
    """Allow loopback only (the local fixture site); everything else raises RuntimeError.

    Documented M1 change to the M0 guard (D12): M0 refused every connect.
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    def _refuse(host: object) -> None:
        raise RuntimeError(f"network access is not allowed in tests (host={host!r})")

    def connect(self, address):  # type: ignore[no-untyped-def]
        if _host_of(address) not in LOOPBACK_HOSTS:
            _refuse(address)
        return real_connect(self, address)

    def connect_ex(self, address):  # type: ignore[no-untyped-def]
        if _host_of(address) not in LOOPBACK_HOSTS:
            _refuse(address)
        return real_connect_ex(self, address)

    def create_connection(address, *args, **kwargs):  # type: ignore[no-untyped-def]
        if _host_of(address) not in LOOPBACK_HOSTS:
            _refuse(address)
        return real_create_connection(address, *args, **kwargs)

    def getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        if host is not None and str(host) not in LOOPBACK_HOSTS:
            _refuse(host)
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Default settings with all paths inside tmp_path."""
    return Settings(
        _env_file=None,
        db_path=tmp_path / "data" / "test.db",
        cache_dir=tmp_path / ".cache",
        output_xlsx=tmp_path / "out.xlsx",
        log_dir=tmp_path / "logs",
    )


@pytest.fixture
def local_site():
    """A threaded http.server on 127.0.0.1:<ephemeral>; routes are added per test."""
    site = LocalSite()
    site.start()
    try:
        yield site
    finally:
        site.stop()
