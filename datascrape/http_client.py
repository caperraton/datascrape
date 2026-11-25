"""Resilient, thread-aware HTTP transport with shared rate-limit coordination."""
from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, Optional, Tuple, Union
from urllib.parse import urlsplit, urlunsplit

import requests

from datascrape.shutdown import ShutdownRequested, shutdown_requested, wait_or_shutdown

logger = logging.getLogger(__name__)


def _safe_url(url: str) -> str:
    """Return a URL without credentials, query strings, or fragments for safe logging."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    if port:
        host = f"{host}:{port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _retry_after_seconds(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class RateLimitCoordinator:
    """One shared pause gate for all workers using the same API identity."""

    _registry: Dict[str, "RateLimitCoordinator"] = {}
    _registry_lock = threading.Lock()

    def __init__(self, key: str):
        self.key = key
        self._condition = threading.Condition()
        self._pause_until = 0.0  # monotonic clock
        self._pause_reason = ""
        self.pause_count = 0

    @classmethod
    def shared(cls, key: str) -> "RateLimitCoordinator":
        with cls._registry_lock:
            coordinator = cls._registry.get(key)
            if coordinator is None:
                coordinator = cls(key)
                cls._registry[key] = coordinator
            return coordinator

    def wait(self) -> None:
        """Block until the shared pause has expired or shutdown is requested."""
        while True:
            with self._condition:
                remaining = self._pause_until - time.monotonic()
            if remaining <= 0:
                return
            if wait_or_shutdown(min(remaining, 30.0)):
                raise ShutdownRequested("Shutdown requested while waiting for API rate-limit reset")

    def pause_for(self, seconds: float, reason: str) -> None:
        seconds = max(0.0, seconds)
        if seconds <= 0:
            return
        now = time.monotonic()
        with self._condition:
            proposed = now + seconds
            if proposed > self._pause_until:
                self._pause_until = proposed
                self._pause_reason = reason
                self.pause_count += 1
                logger.warning(
                    "Rate-limit coordinator pausing shared workers for %.1f seconds: %s",
                    seconds,
                    reason,
                )
                self._condition.notify_all()

    def remaining(self) -> float:
        with self._condition:
            return max(0.0, self._pause_until - time.monotonic())


class ResilientHTTPClient:
    """HTTP client with thread-local sessions, retries, and shared rate-limit pauses.

    Primary quota exhaustion waits for the server's X-RateLimit-Reset timestamp.
    Secondary limits honor Retry-After and otherwise wait at least one minute,
    increasing the wait exponentially. All workers sharing a coordinator pause
    together to prevent a retry storm.
    """

    RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        headers: Optional[Dict[str, str]] = None,
        auth: Optional[Tuple[str, str]] = None,
        max_retries: int = 8,
        base_delay: float = 2.0,
        max_delay: float = 900.0,
        timeout: int = 30,
        verify_ssl: Union[bool, str] = True,
        min_rate_buffer: int = 10,
        coordinator_key: Optional[str] = None,
        token_provider: Optional[Callable[[], str]] = None,
        auth_scheme: str = "Bearer",
    ):
        self._headers = dict(headers or {})
        self._auth = auth
        self._thread_local = threading.local()
        self._sessions: list[requests.Session] = []
        self._sessions_lock = threading.Lock()
        self.max_retries = max(0, int(max_retries))
        self.base_delay = max(0.1, float(base_delay))
        self.max_delay = max(self.base_delay, float(max_delay))
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.min_rate_buffer = max(0, int(min_rate_buffer))
        self.token_provider = token_provider
        self.auth_scheme = auth_scheme
        self.coordinator_key = coordinator_key or f"anonymous:{id(self)}"
        self.coordinator = RateLimitCoordinator.shared(self.coordinator_key)

        self._metrics_lock = threading.Lock()
        self.total_requests = 0
        self.total_retries = 0
        self.rate_limit_pauses = 0
        self.last_rate_remaining = "?"
        self.last_rate_reset = "?"
        self.last_rate_resource = "?"

    def _get_session(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(self._headers)
            if self._auth:
                session.auth = self._auth
            with self._sessions_lock:
                self._sessions.append(session)
            self._thread_local.session = session
        return session

    @property
    def session(self) -> requests.Session:
        """Compatibility accessor; each calling thread receives its own session."""
        return self._get_session()

    def close(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            session.close()
        if hasattr(self._thread_local, "session"):
            del self._thread_local.session

    def _request_headers(self) -> Optional[Dict[str, str]]:
        if not self.token_provider:
            return None
        token = self.token_provider()
        return {"Authorization": f"{self.auth_scheme} {token}"}

    def _increment(self, name: str) -> None:
        with self._metrics_lock:
            setattr(self, name, getattr(self, name) + 1)

    def _observe_rate_headers(self, response: requests.Response, url: str) -> None:
        headers = response.headers
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        resource = headers.get("X-RateLimit-Resource", "?")
        if remaining is None:
            return
        with self._metrics_lock:
            self.last_rate_remaining = remaining
            self.last_rate_reset = reset or "?"
            self.last_rate_resource = resource
        try:
            remaining_count = int(remaining)
            reset_epoch = float(reset) if reset else None
        except (TypeError, ValueError):
            return
        if remaining_count <= self.min_rate_buffer and reset_epoch:
            wait = max(0.0, reset_epoch - time.time() + 1.0)
            if wait > 0:
                self.coordinator.pause_for(
                    wait,
                    f"primary quota {resource} has {remaining_count} requests remaining; reset at {reset}",
                )
                with self._metrics_lock:
                    self.rate_limit_pauses = self.coordinator.pause_count

    def _exponential_delay(self, attempt: int) -> float:
        ceiling = min(self.max_delay, self.base_delay * (2**attempt))
        # Full jitter avoids synchronized workers repeatedly hitting the API.
        return random.uniform(min(0.25, ceiling), ceiling)

    def _rate_limit_delay(self, response: requests.Response, attempt: int) -> Tuple[float, str]:
        retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
        if retry_after is not None:
            return retry_after + random.uniform(0.0, 1.0), "server Retry-After directive"

        remaining = response.headers.get("X-RateLimit-Remaining")
        reset = response.headers.get("X-RateLimit-Reset")
        if remaining == "0" and reset:
            try:
                return max(1.0, float(reset) - time.time() + 1.0), "primary rate limit reset window"
            except ValueError:
                pass

        body = (response.text or "").lower()
        secondary = "secondary rate limit" in body or "abuse detection" in body
        if secondary:
            # GitHub recommends waiting at least one minute for secondary limits.
            return max(60.0, self._exponential_delay(attempt)), "secondary rate limit recovery"
        return max(1.0, self._exponential_delay(attempt)), "HTTP 429 recovery"

    @staticmethod
    def _is_rate_limited(response: requests.Response) -> bool:
        if response.status_code == 429:
            return True
        if response.status_code != 403:
            return False
        body = (response.text or "").lower()
        return (
            response.headers.get("X-RateLimit-Remaining") == "0"
            or bool(response.headers.get("Retry-After"))
            or "rate limit" in body
            or "secondary rate" in body
            or "abuse detection" in body
        )

    def request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        allow_404: bool = False,
        allow_403: bool = False,
        allow_422: bool = False,
    ) -> Optional[requests.Response]:
        safe_url = _safe_url(url)
        for attempt in range(self.max_retries + 1):
            if shutdown_requested():
                raise ShutdownRequested("Shutdown requested before HTTP request")
            self.coordinator.wait()
            with self._metrics_lock:
                self.total_requests += 1
            try:
                response = self._get_session().request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_body,
                    headers=self._request_headers(),
                    timeout=self.timeout,
                    verify=self.verify_ssl,
                )
            except (requests.Timeout, requests.ConnectionError, requests.ChunkedEncodingError) as exc:
                if attempt >= self.max_retries:
                    logger.exception("Network request exhausted retries: %s %s", method, safe_url)
                    raise RuntimeError(f"Network error for {safe_url}: {type(exc).__name__}") from exc
                delay = self._exponential_delay(attempt)
                self._increment("total_retries")
                logger.warning(
                    "Network error on %s %s (%s); retry %s/%s in %.1fs",
                    method,
                    safe_url,
                    type(exc).__name__,
                    attempt + 1,
                    self.max_retries,
                    delay,
                )
                if wait_or_shutdown(delay):
                    raise ShutdownRequested("Shutdown requested during network retry backoff")
                continue

            self._observe_rate_headers(response, safe_url)
            logger.debug("HTTP %s %s -> %s (attempt %s/%s)", method, safe_url, response.status_code, attempt + 1, self.max_retries + 1)

            if response.status_code == 404 and allow_404:
                return None
            if response.status_code == 422 and allow_422:
                return None

            if self._is_rate_limited(response):
                if attempt >= self.max_retries:
                    logger.error("Rate-limit retries exhausted for %s %s", method, safe_url)
                    response.raise_for_status()
                delay, reason = self._rate_limit_delay(response, attempt)
                self._increment("total_retries")
                self.coordinator.pause_for(delay, f"HTTP {response.status_code} {reason} at {safe_url}")
                with self._metrics_lock:
                    self.rate_limit_pauses = self.coordinator.pause_count
                logger.warning(
                    "Retrying rate-limited request %s %s after shared pause (retry %s/%s)",
                    method,
                    safe_url,
                    attempt + 1,
                    self.max_retries,
                )
                continue

            # GitHub's repository statistics endpoints return 202 while computing.
            if response.status_code == 202 and "/stats/" in url:
                if attempt >= self.max_retries:
                    return response
                delay = self._exponential_delay(attempt)
                self._increment("total_retries")
                logger.info("GitHub statistics are still computing; retrying in %.1fs: %s", delay, safe_url)
                if wait_or_shutdown(delay):
                    raise ShutdownRequested("Shutdown requested during GitHub statistics retry backoff")
                continue

            if 200 <= response.status_code < 300:
                return response

            if response.status_code == 403 and allow_403:
                return response

            if response.status_code in self.RETRYABLE_STATUSES:
                if attempt >= self.max_retries:
                    response.raise_for_status()
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                delay = retry_after if retry_after is not None else self._exponential_delay(attempt)
                self._increment("total_retries")
                logger.warning(
                    "Transient HTTP %s from %s; retry %s/%s in %.1fs",
                    response.status_code,
                    safe_url,
                    attempt + 1,
                    self.max_retries,
                    delay,
                )
                if wait_or_shutdown(delay):
                    raise ShutdownRequested("Shutdown requested during transient HTTP retry backoff")
                continue

            response.raise_for_status()

        return None

    def get_rate_limit_state(self) -> Dict[str, Any]:
        with self._metrics_lock:
            return {
                "remaining": self.last_rate_remaining,
                "reset": self.last_rate_reset,
                "resource": self.last_rate_resource,
                "requests": self.total_requests,
                "retries": self.total_retries,
                "pauses": self.coordinator.pause_count,
                "shared_pause_seconds": round(self.coordinator.remaining(), 2),
            }
