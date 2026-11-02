"""ShutdownFlag: request(), signal handlers (first sets the flag, second raises), restore()."""
from __future__ import annotations

import signal
import threading

import pytest

from corpdisc.services.shutdown import SHUTDOWN, ShutdownFlag


def test_request_sets_flag_once():
    f = ShutdownFlag()
    assert f.requested is False and f.stop_check() is None and f.reason is None
    f.request("CUSTOM")
    f.request("LATER")
    assert f.requested and f.stop_check() == SHUTDOWN and f.reason == "CUSTOM"


def test_first_signal_sets_flag_second_raises_and_restore():
    before = signal.getsignal(signal.SIGINT)
    f = ShutdownFlag()
    try:
        assert f.install() is True
        assert signal.getsignal(signal.SIGINT) != before
        signal.raise_signal(signal.SIGINT)
        assert f.requested and f.reason == SHUTDOWN
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGINT)
    finally:
        f.restore()
    assert signal.getsignal(signal.SIGINT) == before


def test_sigterm_and_sigbreak_registered_when_available():
    f = ShutdownFlag()
    names = [n for n in ("SIGTERM", "SIGBREAK") if hasattr(signal, n)]
    originals = {n: signal.getsignal(getattr(signal, n)) for n in names}
    try:
        f.install()
        for n in names:
            assert signal.getsignal(getattr(signal, n)) == f._handle
        f.install()  # idempotent: the original handlers are not overwritten by our own
    finally:
        f.restore()
    for n, orig in originals.items():
        assert signal.getsignal(getattr(signal, n)) == orig


def test_install_refused_outside_main_thread():
    out = []
    f = ShutdownFlag()
    t = threading.Thread(target=lambda: out.append(f.install()))
    t.start()
    t.join()
    assert out == [False] and f._previous == {}


def test_restore_without_install_is_harmless():
    ShutdownFlag().restore()
