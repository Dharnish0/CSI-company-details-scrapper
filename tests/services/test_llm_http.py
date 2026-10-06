"""RequestsJsonTransport against a loopback POST server: statuses, timeout, refusal, size cap, redirects, no secret logs."""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from corpdisc.services.llm_http import RequestsJsonTransport
from corpdisc.transport import TransportFailure

KEY = "sk-test-SECRETSECRET123456"


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:  # skip the slow getfqdn()
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()[:2]
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


@pytest.fixture
def server():
    seen: list[dict] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a, **k):  # silence
            pass

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("content-length", 0))
            seen.append({"path": self.path, "body": self.rfile.read(n).decode(), "auth": self.headers.get("authorization")})
            if self.path == "/slow":
                time.sleep(1.0)
            if self.path == "/redir":
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 503 if self.path == "/503" else 200
            body = (b"x" * 50_000) if self.path == "/big" else json.dumps({"ok": True}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = _Server(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen
    srv.shutdown()
    srv.server_close()


def tx(**kw):
    return RequestsJsonTransport(trust_env=False, **kw)


def test_post_returns_status_headers_and_body(server):
    base, seen = server
    r = tx().post(base + "/ok", headers={"authorization": f"Bearer {KEY}", "content-type": "application/json"},
                  body='{"a":1}', timeout=5)
    assert r.status == 200 and json.loads(r.body) == {"ok": True} and r.headers["retry-after"] == "4"
    assert seen[0]["body"] == '{"a":1}' and seen[0]["auth"] == f"Bearer {KEY}"


def test_error_statuses_are_returned_not_raised(server):
    base, _ = server
    r = tx().post(base + "/503", headers={}, body="{}", timeout=5)
    assert r.status == 503 and r.headers["retry-after"] == "4"


def test_timeout_maps_to_transport_failure(server):
    base, _ = server
    with pytest.raises(TransportFailure) as e:
        tx().post(base + "/slow", headers={}, body="{}", timeout=0.2)
    assert e.value.code == "TIMEOUT"


def test_connection_refused_and_no_secret_in_logs(caplog):
    caplog.set_level(logging.DEBUG)
    # Use a port that is guaranteed to be closed by binding to 0, getting the port,
    # and then closing. On Windows, the port may enter TIME_WAIT state, so we try
    # multiple ports to find one that is truly refused.
    import socket
    for _ in range(3):  # try up to 3 ports to find a truly closed one
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        # Small delay to help ensure port is released (helps on Windows)
        import time as _time
        _time.sleep(0.1)
        try:
            tx().post(f"http://127.0.0.1:{port}/", headers={"authorization": f"Bearer {KEY}"}, body="{}", timeout=2)
            # If we get here, the port was not refused; try another
        except TransportFailure as e:
            # On Windows, TIME_WAIT may cause TIMEOUT instead of CONNECTION error.
            # Accept either code as long as it's a connection-level failure and no secret leaked.
            assert e.code in ("CONNECTION", "TIMEOUT") and KEY not in str(e) and KEY not in caplog.text
            return
    pytest.fail("Could not find a closed port for testing")


def test_response_size_is_capped(server):
    base, _ = server
    with pytest.raises(TransportFailure) as e:
        tx(max_bytes=1000).post(base + "/big", headers={}, body="{}", timeout=5)
    assert e.value.code == "TOO_LARGE"


def test_redirects_are_not_followed(server):
    base, seen = server
    r = tx().post(base + "/redir", headers={"authorization": f"Bearer {KEY}"}, body="{}", timeout=5)
    assert r.status == 302 and [s["path"] for s in seen] == ["/redir"]  # the key is never forwarded to another host
