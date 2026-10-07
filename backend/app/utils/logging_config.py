"""Central logging setup: console + rotating file in logs/, never logs secrets."""
from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler

_SECRET_RE = re.compile(r"(OPENROUTER_API_KEY=|api[_-]?key[\"'=:\s]+)([^&\s\"']+)", re.IGNORECASE)

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs")
os.makedirs(LOG_DIR, exist_ok=True)


class SecretFilter(logging.Filter):
    """Redacts anything that looks like an API key from log output."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args:
            try:
                record.args = tuple(
                    _SECRET_RE.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args
                )
            except Exception:
                pass
        if isinstance(record.msg, str):
            record.msg = _SECRET_RE.sub(r"\1***", record.msg)
        return True


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(SecretFilter())
    root.addHandler(console)

    try:
        file_handler = RotatingFileHandler(
            os.path.join(LOG_DIR, "backend.log"), maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        file_handler.addFilter(SecretFilter())
        root.addHandler(file_handler)
    except OSError:
        console.write = console.flush  # no-op guard; console logging still works
        pass


logger = logging.getLogger("trading")
