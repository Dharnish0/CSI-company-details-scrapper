"""Threaded local HTTP server for tests (127.0.0.1 only, ephemeral port)."""
from __future__ import annotations

import gzip
import mimetypes
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


@dataclass
class Route:
    status: int = 200
    body: bytes = b""
    content_type: str | None = "text/html; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0
    gzip_body: bool = False


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:  # skip the slow getfqdn() in HTTPServer.server_bind
        import socketserver

        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


class LocalSite:
    """Routes map a request path (or path?query) to a Route or a list of Routes (served in order,
    last one repeating), which is how "500 then 200" is scripted."""

    def __init__(self) -> None:
        self._routes: dict[str, list[Route]] = {}
        self._hits: Counter[str] = Counter()
        self._lock = threading.Lock()
        self.request_log: list[tuple[str, dict[str, str]]] = []
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    # -- configuration
    def add(
        self,
        path: str,
        body: str | bytes = "",
        *,
        status: int = 200,
        content_type: str | None = "text/html; charset=utf-8",
        headers: dict[str, str] | None = None,
        delay: float = 0.0,
        gzip_body: bool = False,
    ) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self._routes[path] = [Route(status, data, content_type, dict(headers or {}), delay, gzip_body)]

    def add_sequence(self, path: str, routes: list[Route]) -> None:
        self._routes[path] = list(routes)

    def redirect(self, path: str, location: str, status: int = 302) -> None:
        self.add(path, b"", status=status, headers={"Location": location})

    def serve_dir(self, directory: Path) -> None:
        """Expose every file under ``directory`` at its relative URL path."""
        for file in sorted(directory.rglob("*")):
            if file.is_file():
                rel = "/" + file.relative_to(directory).as_posix()
                ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype == "application/xhtml+xml":
                    ctype += "; charset=utf-8"
                self.add(rel, file.read_bytes(), content_type=ctype)
                if file.name == "index.html" and file.parent == directory:
                    self.add("/", file.read_bytes(), content_type=ctype)

    # -- introspection
    @property
    def base_url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def url(self, path: str = "/") -> str:
        return self.base_url + path

    def hits(self, path: str) -> int:
        with self._lock:
            return self._hits[path]

    @property
    def total_hits(self) -> int:
        with self._lock:
            return sum(self._hits.values())

    def paths_requested(self) -> list[str]:
        with self._lock:
            return [p for p, _ in self.request_log]

    # -- lifecycle
    def _next_route(self, raw_path: str) -> Route | None:
        path = urlsplit(raw_path).path
        with self._lock:
            self._hits[path] += 1
            key = raw_path if raw_path in self._routes else path
            routes = self._routes.get(key)
            if not routes:
                return None
            return routes.pop(0) if len(routes) > 1 else routes[0]

    def start(self) -> None:
        site = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args, **kwargs) -> None:  # silence
                pass

            def do_GET(self) -> None:  # noqa: N802
                with site._lock:
                    site.request_log.append((urlsplit(self.path).path, dict(self.headers)))
                route = site._next_route(self.path)
                if route is None:
                    route = Route(404, b"not found", "text/plain")
                if route.delay:
                    time.sleep(route.delay)
                body = gzip.compress(route.body) if route.gzip_body else route.body
                try:
                    self.send_response(route.status)
                    if route.content_type:
                        self.send_header("Content-Type", route.content_type)
                    if route.gzip_body:
                        self.send_header("Content-Encoding", "gzip")
                    for k, v in route.headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        self._server = _Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
