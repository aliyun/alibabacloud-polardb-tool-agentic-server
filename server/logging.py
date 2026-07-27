from __future__ import annotations

import logging
import re
import sys
import threading
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from server.config import LoggingConfig

trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
session_id_var: ContextVar[str] = ContextVar("session_id", default="")

_LOG_FORMAT = (
    "[%(asctime)s][%(name)s][PID:%(process)d]"
    "[TID:%(trace_id)s][SID:%(session_id)s][%(levelname)s] %(message)s"
)

_STANDARD_RECORD_ATTRS = set(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime", "trace_id", "session_id"}


def _parse_timezone(tz_str: str) -> tuple[timezone, str]:
    """Parse a timezone string like 'UTC+8', 'UTC-5', 'UTC' into a timezone object and label."""
    if tz_str == "UTC":
        return timezone.utc, "UTC"
    m = re.match(r"^UTC([+-])(\d{1,2})$", tz_str)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hours = int(m.group(2))
        tz = timezone(timedelta(hours=sign * hours), name=tz_str)
        return tz, tz_str
    return timezone(timedelta(hours=8), name="UTC+8"), "UTC+8"


class _AgenticServerFormatter(logging.Formatter):
    def __init__(self, tz: timezone | None = None, tz_label: str = "UTC+8") -> None:
        super().__init__(fmt=_LOG_FORMAT)
        self._tz = tz or timezone(timedelta(hours=8), name="UTC+8")
        self._tz_label = tz_label

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=self._tz)
        msec = int(record.msecs) % 1000
        return f"{dt.strftime('%Y-%m-%d %H:%M:%S')},{msec:03d} {self._tz_label}"

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "trace_id") or not record.trace_id:
            record.trace_id = "-"
        if not hasattr(record, "session_id") or not record.session_id:
            record.session_id = "-"
        base = super().format(record)
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _STANDARD_RECORD_ATTRS and v is not None
        }
        if extras:
            return base + " | " + " ".join(
                f"{k}={v}" for k, v in sorted(extras.items())
            )
        return base


class _RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = trace_id_var.get("") or "-"  # type: ignore[attr-defined]
        record.session_id = session_id_var.get("") or "-"  # type: ignore[attr-defined]
        return True


class LogManager:
    def __init__(
        self,
        *,
        log_dir: str | Path = "log",
        filename: str = "alibabacloud-polardb-tool-agentic-server.log",
        max_bytes: int = 100 * 1024 * 1024,
        backup_count: int = 10,
        fallback_dirs: Iterable[str | Path] | None = None,
    ) -> None:
        self._log_dir = Path(log_dir)
        self._filename = filename
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._fallback_dirs = [Path(p) for p in (fallback_dirs or [])]

    def attach_file_handler(
        self,
        logger: logging.Logger,
        *,
        level: int,
        formatter: logging.Formatter,
        context_filter: logging.Filter | None = None,
    ) -> Path | None:
        candidates = [self._log_dir, *self._fallback_dirs]
        for directory in candidates:
            target = self._try_attach(
                logger,
                directory,
                level=level,
                formatter=formatter,
                context_filter=context_filter,
            )
            if target is not None:
                return target
        return None

    def _try_attach(
        self,
        logger: logging.Logger,
        directory: Path,
        *,
        level: int,
        formatter: logging.Formatter,
        context_filter: logging.Filter | None = None,
    ) -> Path | None:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            target = (directory / self._filename).resolve()
        except Exception:
            return None

        for existing in logger.handlers:
            if isinstance(existing, RotatingFileHandler):
                base = getattr(existing, "baseFilename", "")
                if base and Path(base).resolve() == target:
                    return target

        try:
            handler = RotatingFileHandler(
                filename=target,
                maxBytes=self._max_bytes,
                backupCount=self._backup_count,
                encoding="utf-8",
            )
            handler.setLevel(level)
            handler.setFormatter(formatter)
            if context_filter is not None:
                handler.addFilter(context_filter)
            logger.addHandler(handler)
            return target
        except Exception:
            return None


_configured = False
_lock = threading.Lock()

_FALLBACK_DIRS = ("/app/log", "/tmp/polardb-agentic-log")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$", re.ASCII)


def setup_logging(
    level: str = "INFO", logging_config: LoggingConfig | None = None
) -> None:
    with _lock:
        global _configured
        if _configured:
            return
        _configured = True

    level_value = getattr(logging, level.upper(), logging.INFO)

    tz_str = "UTC+8"
    log_dir = "log"
    log_file = "alibabacloud-polardb-tool-agentic-server.log"
    max_bytes = 100 * 1024 * 1024
    backup_count = 10

    if logging_config is not None:
        tz_str = logging_config.timezone
        log_dir = logging_config.log_dir
        log_file = logging_config.log_file
        max_bytes = logging_config.max_bytes
        backup_count = logging_config.backup_count

    tz, tz_label = _parse_timezone(tz_str)
    formatter = _AgenticServerFormatter(tz=tz, tz_label=tz_label)
    context_filter = _RequestContextFilter()

    root = logging.getLogger("server")
    root.setLevel(level_value)
    root.propagate = False

    for handler in list(root.handlers):
        if isinstance(handler, (logging.StreamHandler, logging.FileHandler)):
            root.removeHandler(handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(level_value)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(context_filter)
    root.addHandler(stream_handler)

    manager = LogManager(
        log_dir=log_dir,
        filename=log_file,
        max_bytes=max_bytes,
        backup_count=backup_count,
        fallback_dirs=_FALLBACK_DIRS,
    )
    log_path = manager.attach_file_handler(
        root,
        level=level_value,
        formatter=formatter,
        context_filter=context_filter,
    )

    if log_path:
        root.info(
            "Persistent logging enabled: path=%s max_bytes=%d backup=%d",
            log_path,
            max_bytes,
            backup_count,
        )
    else:
        root.warning(
            "Persistent logging disabled: unable to create file log (tried: %s)",
            ", ".join([log_dir, *[str(d) for d in _FALLBACK_DIRS]]),
        )

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)


def reset_logging() -> None:
    with _lock:
        global _configured
        _configured = False


def generate_request_id() -> str:
    return uuid.uuid4().hex[:16]


def normalize_request_id(value: str | None) -> str:
    """Return a safe request ID, replacing untrusted values rather than truncating."""
    if isinstance(value, str) and _REQUEST_ID_RE.fullmatch(value):
        return value
    return generate_request_id()
