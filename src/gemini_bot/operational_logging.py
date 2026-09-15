from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import logging
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
import queue
import re
from typing import Any, Protocol


class ErrorClass(str, Enum):
    TRANSPORT = "transport"
    PROTOCOL = "protocol"
    RATE_LIMIT = "rate_limit"
    RECONCILIATION = "reconciliation"
    RISK = "risk"
    STORAGE = "storage"
    INVARIANT = "invariant"
    EXECUTION = "execution"
    UNKNOWN = "unknown"


class AuditJournal(Protocol):
    def audit(self, kind: str, details: dict[str, object]) -> None: ...


_SENSITIVE_KEYS = frozenset({
    "api_key", "apikey", "api_secret", "secret", "signature", "payload",
    "authorization", "headers", "x-gemini-apikey", "x-gemini-payload",
    "x-gemini-signature", "x-gemini-nonce",
})
_KEY_PATTERN = re.compile(r"\b(?:account|master)-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE)
_HEADER_PATTERN = re.compile(
    r"(?i)(X-GEMINI-(?:APIKEY|PAYLOAD|SIGNATURE|NONCE)|Authorization)\s*[:=]\s*[^\s,;}]+"
)


class Redactor:
    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        self._secrets = tuple(secret for secret in secrets if secret and len(secret) >= 6)

    def text(self, value: object) -> str:
        result = str(value)
        for secret in self._secrets:
            result = result.replace(secret, "[REDACTED]")
        result = _KEY_PATTERN.sub("[REDACTED_API_KEY]", result)
        result = _HEADER_PATTERN.sub(lambda match: match.group(1) + "=[REDACTED]", result)
        return result

    def value(self, value: Any, key: str | None = None) -> Any:
        if key is not None and key.lower() in _SENSITIVE_KEYS:
            return "[REDACTED]"
        if isinstance(value, dict):
            return {str(item_key): self.value(item_value, str(item_key)) for item_key, item_value in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self.value(item) for item in value]
        if isinstance(value, (str, bytes)):
            return self.text(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return self.text(value)


class JsonFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        event_data = getattr(record, "event_data", {})
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": self.redactor.text(record.getMessage()),
            "process_id": record.process,
            "thread_id": record.thread,
        }
        if isinstance(event_data, dict):
            payload.update(self.redactor.value(event_data))
        if record.exc_info:
            payload["exception"] = self.redactor.text(self.formatException(record.exc_info))
        return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


class DroppingQueueHandler(QueueHandler):
    def __init__(self, target: queue.Queue[logging.LogRecord]) -> None:
        super().__init__(target)
        self.dropped = 0

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        return copy.copy(record)

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1


@dataclass(slots=True)
class LoggingRuntime:
    logger: logging.Logger
    queue_handler: DroppingQueueHandler
    listener: QueueListener
    file_handler: RotatingFileHandler

    @property
    def dropped_records(self) -> int:
        return self.queue_handler.dropped

    def close(self) -> None:
        self.logger.removeHandler(self.queue_handler)
        # QueueListener.stop uses put_nowait; a full bounded queue must drain
        # before the sentinel can be inserted during shutdown.
        while True:
            try:
                self.listener.stop()
                break
            except queue.Full:
                import time
                time.sleep(0.01)
        self.queue_handler.close()
        self.file_handler.close()


class OperationalLogger:
    def __init__(self, runtime: LoggingRuntime, journal: AuditJournal | None = None) -> None:
        self.runtime = runtime
        self.journal = journal

    def event(self, level: int, event: str, **fields: object) -> None:
        self.runtime.logger.log(level, event, extra={"event_data": fields})
        if level >= logging.ERROR and self.journal is not None:
            try:
                safe = self.runtime.file_handler.formatter.redactor.value(fields)  # type: ignore[attr-defined]
                self.journal.audit(event, safe)
            except Exception:
                self.runtime.logger.error(
                    "critical_event_journal_failed",
                    extra={"event_data": {"original_event": event, "error_class": ErrorClass.STORAGE.value}},
                )

    def exception(self, event: str, error_class: ErrorClass, exc: BaseException, **fields: object) -> None:
        fields = {**fields, "error_class": error_class.value, "exception_type": type(exc).__name__}
        self.runtime.logger.error(
            event,
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={"event_data": fields},
        )
        if self.journal is not None:
            try:
                safe = self.runtime.file_handler.formatter.redactor.value(fields)  # type: ignore[attr-defined]
                self.journal.audit(event, safe)
            except Exception:
                self.runtime.logger.error(
                    "critical_event_journal_failed",
                    extra={"event_data": {"original_event": event, "error_class": ErrorClass.STORAGE.value}},
                )


def configure_logging(
    directory: Path,
    *,
    secrets: tuple[str, ...] = (),
    queue_capacity: int = 4096,
    max_bytes: int = 20 * 1024 * 1024,
    backups: int = 5,
) -> LoggingRuntime:
    if queue_capacity <= 0 or max_bytes <= 0 or backups < 1:
        raise ValueError("invalid logging bounds")
    directory.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        directory / "gemini-bot.jsonl",
        maxBytes=max_bytes,
        backupCount=backups,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(JsonFormatter(Redactor(secrets)))
    records: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=queue_capacity)
    queue_handler = DroppingQueueHandler(records)
    logger = logging.getLogger(f"gemini_bot.production.{id(records)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    logger.addHandler(queue_handler)
    listener = QueueListener(records, file_handler, respect_handler_level=True)
    listener.start()
    return LoggingRuntime(logger, queue_handler, listener, file_handler)
