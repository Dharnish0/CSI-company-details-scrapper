"""Logging foundation (stdlib logging only): console + file, with redaction.

Secrets configured in Settings and common key=value / Bearer patterns are masked,
and very long messages (for example page bodies) are truncated.
"""

from __future__ import annotations

import logging
import re
import sys
import traceback
from collections.abc import Iterable

from corpdisc.config import Settings

ROOT_LOGGER_NAME = "corpdisc"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
MAX_MESSAGE_CHARS = 2000
MASK = "***"

_MANAGED_ATTR = "_corpdisc_managed"
_KV_SECRET_RE = re.compile(
    r"""(?ix)
    ([\w-]*(?:api[_-]?key|token|secret|password|authorization)[\w-]*)   # key name
    (\s*[=:]\s*)                                                        # separator
    (?:Bearer\s+)?                                                      # optional scheme
    ("[^"]*"|'[^']*'|[^\s,;&]+)                                         # value
    """
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Mask configured secret values and common secret patterns."""
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, MASK)
    text = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)
    return _BEARER_RE.sub(f"Bearer {MASK}", text)


class RedactionFilter(logging.Filter):
    """Redacts and truncates the fully formatted message (args included)."""

    def __init__(self, secrets: Iterable[str] = (), max_chars: int = MAX_MESSAGE_CHARS) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]
        self._max_chars = max_chars

    def filter(self, record: logging.LogRecord) -> bool:
        message = redact(record.getMessage(), self._secrets)
        if len(message) > self._max_chars:
            extra = len(message) - self._max_chars
            message = f"{message[: self._max_chars]}...[truncated {extra} chars]"
        record.msg = message
        record.args = None
        if record.exc_info and record.exc_info[0] is not None:
            text = "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
            record.exc_text = redact(text, self._secrets)
        return True


def get_logger(name: str = ROOT_LOGGER_NAME) -> logging.Logger:
    """Return a logger under the corpdisc namespace."""
    if name != ROOT_LOGGER_NAME and not name.startswith(ROOT_LOGGER_NAME + "."):
        name = f"{ROOT_LOGGER_NAME}.{name}"
    return logging.getLogger(name)


def shutdown_logging() -> None:
    """Remove and close the handlers installed by setup_logging (releases file locks)."""
    root = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in list(root.handlers):
        if getattr(handler, _MANAGED_ATTR, False):
            root.removeHandler(handler)
            handler.close()


def setup_logging(settings: Settings, log_file_name: str = "corpdisc.log") -> logging.Logger:
    """Configure console and file logging. Safe to call repeatedly (no duplicate handlers)."""
    shutdown_logging()
    log_dir = settings.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    level = logging.getLevelName(settings.log_level)
    formatter = logging.Formatter(LOG_FORMAT)
    redaction = RedactionFilter(settings.secret_values())

    console = logging.StreamHandler(sys.stderr)
    file_handler = logging.FileHandler(log_dir / log_file_name, encoding="utf-8")
    root = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in (console, file_handler):
        handler.setLevel(level)
        handler.setFormatter(formatter)
        handler.addFilter(redaction)
        setattr(handler, _MANAGED_ATTR, True)
        root.addHandler(handler)

    root.setLevel(level)
    root.propagate = False
    return root
