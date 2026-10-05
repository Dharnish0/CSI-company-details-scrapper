"""``requests``-backed JsonTransport for LLM providers: bounded, no redirects, never logs headers or bodies."""

from __future__ import annotations

from collections.abc import Mapping

import requests

from corpdisc.logging_setup import get_logger
from corpdisc.transport import HttpReply, TransportFailure

log = get_logger("services.llm_http")


class RequestsJsonTransport:
    def __init__(self, *, max_bytes: int = 4_000_000, verify_ssl: bool = True, trust_env: bool = True) -> None:
        self._session = requests.Session()
        self._session.trust_env = trust_env
        self._session.verify = verify_ssl
        self._max = max_bytes

    def close(self) -> None:
        self._session.close()

    def post(self, url: str, *, headers: Mapping[str, str], body: str, timeout: float) -> HttpReply:
        try:
            r = self._session.post(url, data=body.encode("utf-8"), headers=dict(headers), timeout=timeout,
                                   allow_redirects=False, stream=True)
            chunks, size = [], 0
            for chunk in r.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > self._max:
                    r.close()
                    raise TransportFailure("TOO_LARGE", "response exceeded the size cap")
                chunks.append(chunk)
            text = b"".join(chunks).decode(r.encoding or "utf-8", errors="replace")
            return HttpReply(r.status_code, {k.lower(): v for k, v in r.headers.items()}, text)
        except requests.Timeout as exc:
            raise TransportFailure("TIMEOUT", "request timed out") from exc
        except requests.exceptions.SSLError as exc:
            raise TransportFailure("SSL", "TLS failure") from exc
        except requests.RequestException as exc:
            log.warning("LLM transport error: %s", type(exc).__name__)
            raise TransportFailure("CONNECTION", type(exc).__name__) from exc
