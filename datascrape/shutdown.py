"""Cooperative shutdown signal shared by HTTP workers and batch orchestration."""
from __future__ import annotations

import threading

_shutdown_event = threading.Event()


class ShutdownRequested(Exception):
    """Raised inside workers when the process is asked to stop cooperatively."""


def request_shutdown() -> None:
    _shutdown_event.set()


def clear_shutdown() -> None:
    _shutdown_event.clear()


def shutdown_requested() -> bool:
    return _shutdown_event.is_set()


def wait_or_shutdown(seconds: float) -> bool:
    """Wait up to seconds; return True when shutdown was requested."""
    return _shutdown_event.wait(max(0.0, seconds))
