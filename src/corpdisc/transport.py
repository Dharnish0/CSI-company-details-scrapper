"""Neutral JSON-over-HTTP transport types for LLM providers (stdlib only).

``agents/providers`` may not import ``requests``/sockets (import-boundary rule), and ``services`` may not import
``agents``. Both sides therefore share this tiny module: providers depend on the ``JsonTransport`` protocol, and
``services/llm_http.py`` implements it with ``requests``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class HttpReply:
    status: int
    headers: Mapping[str, str]  # lowercase keys
    body: str


class TransportFailure(Exception):
    """Network-level failure before any HTTP status was received. The message never contains headers or keys."""

    def __init__(self, code: Literal["TIMEOUT", "CONNECTION", "SSL", "TOO_LARGE"], message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class JsonTransport(Protocol):
    def post(self, url: str, *, headers: Mapping[str, str], body: str, timeout: float) -> HttpReply: ...
