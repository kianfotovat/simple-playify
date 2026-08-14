"""Rotating logs and shared secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from collections import deque
from logging.handlers import RotatingFileHandler
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .constants import LOG_DIR, ensure_runtime_dirs

_DISCORD_TOKEN = re.compile(r"(?i)\b[MN][A-Za-z\d_-]{20,}\.[A-Za-z\d_-]{6,}\.[A-Za-z\d_-]{20,}\b")
_AUTH_HEADER = re.compile(r"(?i)(authorization\s*[:=]\s*)([^\s,;]+)")
_KEY_VALUE_SECRET = re.compile(
    r"(?i)(token|secret|password|cookie|api[_-]?key)(\s*[:=]\s*)([^\s,;]+)"
)
_URL = re.compile(r"https?://[^\s<>]+")


def _redact_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in ").,]}":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    try:
        parts = urlsplit(raw)
        hostname = parts.hostname or ""
        netloc = hostname
        if parts.port:
            netloc += f":{parts.port}"
        safe = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
        return safe + trailing
    except ValueError:
        return "[redacted-url]" + trailing


def redact(value: Any) -> str:
    text = str(value)
    text = _DISCORD_TOKEN.sub("[redacted-token]", text)
    text = _AUTH_HEADER.sub(r"\1[redacted]", text)
    text = _KEY_VALUE_SECRET.sub(r"\1\2[redacted]", text)
    return _URL.sub(_redact_url, text)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class JsonStdoutHandler(logging.Handler):
    """Emit prefixed JSON that the parent TUI can parse without ambiguity."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = {
                "type": "log",
                "level": record.levelname,
                "logger": record.name,
                "message": redact(record.getMessage()),
            }
            sys.stdout.write("PLAYIFY_EVENT " + json.dumps(payload, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception:
            self.handleError(record)


class MemoryLogHandler(logging.Handler):
    def __init__(self, maximum: int = 2_000) -> None:
        super().__init__()
        self.entries: deque[str] = deque(maxlen=maximum)

    def emit(self, record: logging.LogRecord) -> None:
        self.entries.append(redact(self.format(record)))


def configure_logging(*, bot_process: bool = False) -> MemoryLogHandler:
    ensure_runtime_dirs()
    formatter = RedactingFormatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    playify_handler = RotatingFileHandler(
        LOG_DIR / "playify.log", maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8"
    )
    playify_handler.setFormatter(formatter)
    root.addHandler(playify_handler)

    if bot_process:
        bot_handler = RotatingFileHandler(
            LOG_DIR / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8"
        )
        bot_handler.setFormatter(formatter)
        root.addHandler(bot_handler)
        root.addHandler(JsonStdoutHandler())
    else:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)

    memory = MemoryLogHandler()
    memory.setFormatter(formatter)
    root.addHandler(memory)
    return memory
