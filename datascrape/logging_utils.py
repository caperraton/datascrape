"""Centralized, secret-aware logging configuration for Datascrape."""
from __future__ import annotations

import logging
import re
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|token|basic)\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:access[_-]?token|refresh[_-]?token|token|password|secret|private[_-]?key)\s*[:=]\s*['\"]?)[^\s,;'\"]+"),
    re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+=*"),
    re.compile(r"(?i)([?&](?:access_token|token|api_key|password|secret)=)[^&\s]+"),
    re.compile(r"(?i)(gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}"),
    re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@"),
)


class SecretRedactionFilter(logging.Filter):
    """Redact common credential formats from messages and exception traces."""

    @staticmethod
    def _redact(value: str) -> str:
        for pattern in _SECRET_PATTERNS:
            value = pattern.sub(r"\1[REDACTED]", value)
        value = re.sub(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            "[REDACTED PRIVATE KEY]",
            value,
            flags=re.DOTALL,
        )
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(record.msg)
        record.msg = self._redact(rendered)
        record.args = ()
        if record.exc_info:
            record.exc_text = self._redact("".join(traceback.format_exception(*record.exc_info)))
        if record.stack_info:
            record.stack_info = self._redact(record.stack_info)
        return True


def configure_logging(
    log_dir: Path = Path("./logs"),
    level: str = "INFO",
    log_file: Optional[str] = None,
    file_logging: bool = True,
) -> Optional[Path]:
    """Configure console and timestamped rotating file logging.

    Returns the active log path, or ``None`` when file logging is disabled.
    """
    numeric_level = getattr(logging, str(level).upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {level}. Use DEBUG, INFO, WARNING, ERROR, or CRITICAL.")

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    root.setLevel(numeric_level)

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s [%(threadName)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redactor = SecretRedactionFilter()

    console = logging.StreamHandler()
    console.setLevel(numeric_level)
    console.setFormatter(formatter)
    console.addFilter(redactor)
    root.addHandler(console)

    active_path: Optional[Path] = None
    if file_logging:
        log_dir.mkdir(parents=True, exist_ok=True)
        filename = log_file or f"datascrape_{datetime.now().astimezone():%Y%m%d_%H%M%S_%f}.log"
        active_path = Path(filename)
        if not active_path.is_absolute():
            active_path = log_dir / active_path
        active_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            active_path,
            maxBytes=20 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redactor)
        root.addHandler(file_handler)

    logging.captureWarnings(True)
    logging.getLogger(__name__).debug(
        "Logging configured: level=%s file=%s", level.upper(), active_path or "disabled"
    )
    return active_path


def prune_files(directory: Path, pattern: str, retention_days: Optional[int]) -> int:
    """Delete matching generated artifacts older than retention_days.

    A missing retention value disables cleanup. Only files matching the caller's
    explicit pattern are considered.
    """
    if not retention_days or retention_days < 1 or not directory.exists():
        return 0
    cutoff = datetime.now().timestamp() - retention_days * 86400
    removed = 0
    for path in directory.glob(pattern):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            logging.getLogger(__name__).warning("Could not prune old artifact %s", path, exc_info=True)
    return removed
