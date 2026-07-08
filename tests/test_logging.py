from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from server.config import LoggingConfig
from server.logging import (
    _AgenticServerFormatter,
    _RequestContextFilter,
    LogManager,
    generate_request_id,
    reset_logging,
    setup_logging,
    trace_id_var,
    session_id_var,
)


@pytest.fixture(autouse=True)
def _reset_logging_state():
    reset_logging()
    yield
    reset_logging()


LOG_PATTERN = re.compile(
    r"^\[[\d\-: ,]+UTC[+\-]?\d*\]"
    r"\[[\w.]+\]"
    r"\[PID:\d+\]"
    r"\[TID:[^\]]+\]"
    r"\[SID:[^\]]+\]"
    r"\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\] .+"
)


class TestAgenticServerFormatter:
    def test_basic_format(self):
        formatter = _AgenticServerFormatter()
        record = logging.LogRecord(
            name="server.test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test message",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        assert LOG_PATTERN.match(output), f"Format mismatch: {output}"
        assert "test message" in output
        assert "[TID:-]" in output
        assert "[SID:-]" in output

    def test_contextvar_injection(self):
        formatter = _AgenticServerFormatter()
        ctx_filter = _RequestContextFilter()
        t_token = trace_id_var.set("trace-abc")
        s_token = session_id_var.set("sess-123")
        try:
            record = logging.LogRecord(
                name="server.test",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg="ctx test",
                args=(),
                exc_info=None,
            )
            ctx_filter.filter(record)
            output = formatter.format(record)
            assert "[TID:trace-abc]" in output
            assert "[SID:sess-123]" in output
        finally:
            trace_id_var.reset(t_token)
            session_id_var.reset(s_token)

    def test_contextvar_defaults(self):
        formatter = _AgenticServerFormatter()
        ctx_filter = _RequestContextFilter()
        record = logging.LogRecord(
            name="server.test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="default test",
            args=(),
            exc_info=None,
        )
        ctx_filter.filter(record)
        output = formatter.format(record)
        assert "[TID:-]" in output
        assert "[SID:-]" in output

    def test_extra_fields_rendered(self):
        formatter = _AgenticServerFormatter()
        record = logging.LogRecord(
            name="server.test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="sql done",
            args=(),
            exc_info=None,
        )
        record.action = "startup"  # type: ignore[attr-defined]
        record.duration_ms = 42  # type: ignore[attr-defined]
        output = formatter.format(record)
        assert "| " in output
        assert "action=startup" in output
        assert "duration_ms=42" in output

    def test_novel_extra_field_captured(self):
        formatter = _AgenticServerFormatter()
        record = logging.LogRecord(
            name="server.test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="novel",
            args=(),
            exc_info=None,
        )
        record.db_name = "mydb"  # type: ignore[attr-defined]
        output = formatter.format(record)
        assert "db_name=mydb" in output

    def test_no_extras_no_pipe(self):
        formatter = _AgenticServerFormatter()
        record = logging.LogRecord(
            name="server.test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="plain message",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        assert "| " not in output

    def test_exception_traceback(self):
        formatter = _AgenticServerFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = logging.LogRecord(
                name="server.test",
                level=logging.ERROR,
                pathname="",
                lineno=0,
                msg="error happened",
                args=(),
                exc_info=sys.exc_info(),
            )
        output = formatter.format(record)
        assert "error happened" in output
        assert "ValueError: boom" in output
        assert "Traceback" in output

    def test_utc_timezone(self):
        from datetime import timezone as tz

        formatter = _AgenticServerFormatter(tz=tz.utc, tz_label="UTC")
        record = logging.LogRecord(
            name="server.test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="tz test",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        assert "UTC]" in output
        assert "UTC+8" not in output


class TestSetupLogging:
    def test_sets_log_level(self):
        setup_logging("debug")
        logger = logging.getLogger("server")
        assert logger.level == logging.DEBUG

    def test_idempotent_no_duplicate_handlers(self):
        setup_logging("info")
        logger = logging.getLogger("server")
        count_after_first = len(logger.handlers)
        setup_logging("info")
        assert len(logger.handlers) == count_after_first

    def test_reset_allows_reconfiguration(self):
        setup_logging("info")
        logger = logging.getLogger("server")
        assert logger.level == logging.INFO
        reset_logging()
        for h in list(logger.handlers):
            logger.removeHandler(h)
        setup_logging("debug")
        assert logger.level == logging.DEBUG


class TestLogManager:
    def test_creates_log_file(self, tmp_path: Path):
        log_dir = tmp_path / "logs"
        manager = LogManager(log_dir=log_dir, filename="test.log")
        logger = logging.getLogger("test_logmanager")
        formatter = _AgenticServerFormatter()
        path = manager.attach_file_handler(
            logger, level=logging.INFO, formatter=formatter
        )
        assert path is not None
        assert path.exists() or path.parent.exists()
        file_handlers = [
            h for h in logger.handlers if isinstance(h, RotatingFileHandler)
        ]
        assert len(file_handlers) == 1
        assert file_handlers[0].maxBytes == 100 * 1024 * 1024
        assert file_handlers[0].backupCount == 10
        for h in logger.handlers[:]:
            logger.removeHandler(h)

    def test_custom_rotation_params(self, tmp_path: Path):
        manager = LogManager(
            log_dir=tmp_path, filename="rot.log", max_bytes=1024, backup_count=3
        )
        logger = logging.getLogger("test_rotation")
        formatter = _AgenticServerFormatter()
        manager.attach_file_handler(logger, level=logging.INFO, formatter=formatter)
        file_handlers = [
            h for h in logger.handlers if isinstance(h, RotatingFileHandler)
        ]
        assert file_handlers[0].maxBytes == 1024
        assert file_handlers[0].backupCount == 3
        for h in logger.handlers[:]:
            logger.removeHandler(h)

    def test_idempotent_attach(self, tmp_path: Path):
        manager = LogManager(log_dir=tmp_path, filename="idem.log")
        logger = logging.getLogger("test_idem")
        formatter = _AgenticServerFormatter()
        manager.attach_file_handler(logger, level=logging.INFO, formatter=formatter)
        manager.attach_file_handler(logger, level=logging.INFO, formatter=formatter)
        file_handlers = [
            h for h in logger.handlers if isinstance(h, RotatingFileHandler)
        ]
        assert len(file_handlers) == 1
        for h in logger.handlers[:]:
            logger.removeHandler(h)

    def test_fallback_directory(self, tmp_path: Path):
        bad_dir = tmp_path / "no_perms"
        bad_dir.mkdir()
        bad_dir.chmod(0o000)
        fallback_dir = tmp_path / "fallback"
        manager = LogManager(
            log_dir=bad_dir / "subdir",
            filename="fb.log",
            fallback_dirs=[fallback_dir],
        )
        logger = logging.getLogger("test_fallback")
        formatter = _AgenticServerFormatter()
        path = manager.attach_file_handler(
            logger, level=logging.INFO, formatter=formatter
        )
        bad_dir.chmod(0o755)
        if path is not None:
            assert "fallback" in str(path)
        for h in logger.handlers[:]:
            logger.removeHandler(h)


class TestGenerateRequestID:
    def test_returns_hex_string(self):
        rid = generate_request_id()
        assert len(rid) == 16
        int(rid, 16)


class TestSetupLoggingWithConfig:
    def test_file_output(self, tmp_path: Path):
        config = LoggingConfig(
            log_dir=str(tmp_path),
            log_file="test-server.log",
        )
        setup_logging("info", config)
        logger = logging.getLogger("server")
        logger.info("file output test")
        log_file = tmp_path / "test-server.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "file output test" in content
        assert LOG_PATTERN.match(content.strip().split("\n")[-1])

    def test_timezone_config(self, tmp_path: Path):
        config = LoggingConfig(
            log_dir=str(tmp_path),
            timezone="UTC",
        )
        setup_logging("info", config)
        logger = logging.getLogger("server")
        logger.info("tz config test")
        log_file = tmp_path / "alibabacloud-polardb-tool-agentic-server.log"
        content = log_file.read_text()
        assert "UTC]" in content
        assert "UTC+8" not in content
