from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (
            "action",
            "artifact_count",
            "attempt",
            "completed_count",
            "domain",
            "elapsed_seconds",
            "error_message",
            "error_type",
            "failed_count",
            "fingerprint",
            "kind",
            "interrupted_count",
            "path",
            "pending_count",
            "phase",
            "progress_current",
            "progress_percent",
            "progress_total",
            "recorded_count",
            "resource_class",
            "run_id",
            "requested_count",
            "skipped_count",
            "status",
            "task_id",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(log_file: Path, name: str) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


@contextmanager
def logged_action(logger: logging.Logger, action: str, **fields: object) -> Iterator[None]:
    start = time.monotonic()
    extra = {"action": action, **fields}
    logger.info("started", extra=extra)
    try:
        yield
    except BaseException as error:
        interrupted = isinstance(error, (KeyboardInterrupt, SystemExit))
        fields = {
            **extra,
            "status": "interrupted" if interrupted else "failed",
            "elapsed_seconds": round(time.monotonic() - start, 3),
            "error_type": type(error).__name__,
            "error_message": str(error),
        }
        if interrupted:
            logger.warning("interrupted", extra=fields)
        else:
            logger.exception("failed", extra=fields)
        raise
    logger.info(
        "completed",
        extra={**extra, "elapsed_seconds": round(time.monotonic() - start, 3)},
    )
