"""Run manifest generation and safe serialization helpers."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

from datascrape import __version__
from datascrape.logging_utils import SecretRedactionFilter

_SECRET_ARGUMENTS = {"token", "private_key", "app_private_key", "password", "secret", "vars", "query"}


def safe_arguments(arguments: Mapping[str, Any]) -> Dict[str, Any]:
    safe: Dict[str, Any] = {}
    for key, value in arguments.items():
        if key.lower() in _SECRET_ARGUMENTS:
            safe[key] = "[REDACTED]" if value else ""
        elif isinstance(value, Path):
            safe[key] = str(value)
        elif key.lower() in {"url", "base_url"} and isinstance(value, str):
            parts = urlsplit(value)
            host = parts.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            try:
                port = parts.port
            except ValueError:
                port = None
            if port:
                host = f"{host}:{port}"
            safe[key] = urlunsplit((parts.scheme, host, parts.path, "", ""))
        elif key.lower() == "param" and isinstance(value, (list, tuple)):
            safe[key] = [
                f"{item.split('=', 1)[0]}=[REDACTED]" if "=" in str(item) and any(secret in str(item).split("=", 1)[0].lower() for secret in ("token", "key", "password", "secret")) else str(item)
                for item in value
            ]
        elif isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
        elif isinstance(value, (list, tuple)):
            safe[key] = [str(item) for item in value]
        else:
            safe[key] = str(value)
    return safe


def write_run_manifest(
    path: Path,
    *,
    run_id: str,
    status: str,
    started_at: str,
    finished_at: Optional[str],
    arguments: Mapping[str, Any],
    reports: list[str],
    outputs: list[str],
    summary: Mapping[str, Any],
    log_file: Optional[Path],
    error: Optional[str] = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "application": "datascrape",
        "version": __version__,
        "run_id": run_id,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "arguments": safe_arguments(arguments),
        "reports": reports,
        "outputs": outputs,
        "summary": dict(summary),
        "log_file": str(log_file) if log_file else None,
        "error": SecretRedactionFilter._redact(error) if error else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
