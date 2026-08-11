"""Structured logging.

JSON in production so the CDN log drain can parse it; human-readable locally.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from app.config import settings
from app.tracing import TraceFilter

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Anything passed via `extra=` rides along.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class HumanFormatter(logging.Formatter):
    """Local development format, with the trace id when one is active.

    Appended rather than inserted into the format string: a `%(run_id)s` in the
    format would raise `KeyError` on every record logged outside a pipeline run
    (API handlers, startup), and `logging` swallows formatter errors into stderr
    noise that is worse than the missing field.
    """

    _BASE = "%(asctime)s  %(levelname)-7s %(name)-24s %(message)s"

    def __init__(self) -> None:
        super().__init__(self._BASE)

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        run_id = getattr(record, "run_id", None)
        if run_id:
            line = f"{line}  [{run_id}]"
        return line


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    if settings.is_production:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(HumanFormatter())

    # Attached to the handler, so it annotates every record that reaches our
    # output regardless of which logger emitted it — including `app/eo/*`, which
    # knows nothing about pipeline runs.
    handler.addFilter(TraceFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # These are chatty and rarely tell us anything we don't already log.
    for noisy in ("httpx", "httpcore", "rasterio", "urllib3", "botocore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
