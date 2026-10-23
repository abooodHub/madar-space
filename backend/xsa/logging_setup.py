"""Structured logging.

* Every record carries a category tag ([SPACE], [BROWSER], [RECORDER], ...).
* Console output is human readable; a JSON-lines file is written for machines.
* Identical messages repeated within a window are collapsed ("repeated N times").
* Known secret patterns (bearer tokens, auth_token/ct0 cookies, api keys) are redacted.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import time
from pathlib import Path

CATEGORIES = (
    "SPACE",
    "MONITOR",
    "BROWSER",
    "RECORDER",
    "AUDIO",
    "JOB",
    "TRANSCRIBE",
    "ANALYSIS",
    "API",
    "DB",
    "SYSTEM",
    "HEALTH",
)

_SECRET_PATTERNS = [
    (re.compile(r"(Bearer\s+)[A-Za-z0-9%._\-]+", re.I), r"\1***"),
    (
        re.compile(
            r"((?:auth_token|ct0|kdt|twid|guest_id|api[_-]?key|token|password)[\"']?\s*[:=]\s*[\"']?)[^\s\"';,&]+",
            re.I,
        ),
        r"\1***",
    ),
    (re.compile(r"(hf_)[A-Za-z0-9]{10,}"), r"\1***"),
    (re.compile(r"(sk-)[A-Za-z0-9]{10,}"), r"\1***"),
]


def redact(text: str) -> str:
    for pat, rep in _SECRET_PATTERNS:
        text = pat.sub(rep, text)
    return text


class _CategoryFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "category"):
            name = record.name.rsplit(".", 1)[-1].upper()
            record.category = name if name in CATEGORIES else "SYSTEM"
        record.msg = redact(str(record.msg))
        if record.args:
            record.args = tuple(
                redact(str(a)) if isinstance(a, str) else a
                for a in (record.args if isinstance(record.args, tuple) else (record.args,))
            )
        return True


class _DedupeFilter(logging.Filter):
    """Suppress identical (category, level, message) within `window` seconds."""

    def __init__(self, window: float = 60.0) -> None:
        super().__init__()
        self.window = window
        self._last: dict[tuple, tuple[float, int]] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        key = (getattr(record, "category", ""), record.levelno, record.getMessage())
        now = time.monotonic()
        prev = self._last.get(key)
        if prev and now - prev[0] < self.window:
            self._last[key] = (prev[0], prev[1] + 1)
            return False
        if prev and prev[1] > 0:
            record.msg = f"{record.getMessage()} (repeated {prev[1]} more times)"
            record.args = ()
        self._last[key] = (now, 0)
        if len(self._last) > 2000:
            self._last.clear()
        return True


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        base = f"{ts} {record.levelname:<7} [{record.category}] {record.getMessage()}"
        extra = getattr(record, "fields", None)
        if extra:
            base += " | " + " ".join(f"{k}={redact(str(v))}" for k, v in extra.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "category": record.category,
            "msg": record.getMessage(),
            "logger": record.name,
        }
        if getattr(record, "fields", None):
            doc["fields"] = {k: redact(str(v)) for k, v in record.fields.items()}
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, ensure_ascii=False)


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    cat = _CategoryFilter()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(_ConsoleFormatter())
    console.addFilter(cat)
    console.addFilter(_DedupeFilter())
    root.addHandler(console)
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_dir / "xsa.jsonl", maxBytes=20_000_000, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(_JsonFormatter())
        fh.addFilter(cat)
        fh.addFilter(_DedupeFilter())
        root.addHandler(fh)
    for noisy in ("httpx", "httpcore", "asyncio", "uvicorn.access", "playwright"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class CatLogger(logging.LoggerAdapter):
    """logger.info("Detected live Space", host="@x", space_id="...")"""

    def process(self, msg, kwargs):
        fields = {
            k: kwargs.pop(k)
            for k in list(kwargs)
            if k not in ("exc_info", "stack_info", "stacklevel", "extra")
        }
        extra = kwargs.setdefault("extra", {})
        extra["category"] = self.extra["category"]
        if fields:
            extra["fields"] = fields
        return msg, kwargs


def get_logger(category: str) -> CatLogger:
    category = category.upper()
    return CatLogger(logging.getLogger(f"xsa.{category.lower()}"), {"category": category})
