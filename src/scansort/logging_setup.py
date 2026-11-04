"""Logging setup. Document text must never be logged; secrets are SecretStr and never formatted."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_STD_ATTRS = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime", "color_message", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS and not k.startswith("_")
        }
        if extras:
            base += " " + " ".join(f"{k}={v}" for k, v in extras.items())
        return base


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # quiet noisy libraries
    for name in ("httpx", "httpcore", "watchdog", "apscheduler", "pypdf", "ocrmypdf", "PIL"):
        logging.getLogger(name).setLevel(logging.WARNING)
