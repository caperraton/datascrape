import random
import threading
import time
from typing import Any, Dict, Optional, Tuple, Union
import requests


class ResilientHTTPClient:
    """Thread-safe HTTP client with proactive/reactive rate-limit pauses and exponential backoff."""

    def __init__(
        self,
        headers: Optional[Dict[str, str]] = None,
        auth: Optional[Tuple[str, str]] = None,
        max_retries: int = 6,
        base_delay: float = 1.5,
        max_delay: float = 180.0,
        timeout: int = 30,
        verify_ssl: Union[bool, str] = True,
        min_rate_buffer: int = 10,
    ):
        self.session = requests.Session()
        if headers:
            self.session.headers.update(headers)
        if auth:
            self.session.auth = auth
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.min_rate_buffer = min_rate_buffer

        self._lock = threading.Lock()
        self._pause_until = 0.0
        self.total_requests = 0
        self.total_retries = 0
        self.rate_limit_pauses = 0
        self.last_rate_remaining = "?"

    def _wait_if_paused(self) -> None:
        while True:
            with self._lock:
                remaining = self._pause_until - time.time()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 5.0))

    def _set_global_pause(self, seconds: float, reason: str) -> None:
        wait = max(1.0, seconds)
        with self._lock:
            target = time.time() + wait
            if target > self._pause_until:
                self._pause_until = target
                self.rate_limit_pauses += 1
                print(f"\n[rate-limit] {reason} -> pausing all workers for {wait:.1f}s")

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
        for attempt in range(self.max_retries + 1):
            self._wait_if_paused()
            with self._lock:
                self.total_requests += 1

            try:
                resp = self.session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_body,
                    timeout=self.timeout,
                    verify=self.verify_ssl,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == self.max_retries:
                    raise RuntimeError(f"Network error for {url}: {exc}") from exc
                self._backoff(attempt, f"network error ({type(exc).__name__})")
                continue

            # Proactive primary rate-limit tracking
            rem = resp.headers.get("X-RateLimit-Remaining")
            reset = resp.headers.get("X-RateLimit-Reset")
            if rem is not None and rem.isdigit():
                with self._lock:
                    self.last_rate_remaining = rem
                if int(rem) <= self.min_rate_buffer and reset and reset.isdigit():
                    wait = max(1.0, float(reset) - time.time() + 2.0)
                    self._set_global_pause(wait, f"primary quota low ({rem} left)")

            if resp.status_code == 404 and allow_404:
                return None
            if resp.status_code == 422 and allow_422:
                return None

            # GitHub /stats/* endpoints return 202 while computing cache
            if resp.status_code == 202 and "/stats/" in url:
                if attempt < self.max_retries:
                    self._backoff(attempt, f"HTTP 202 stats computing on {url}")
                    continue
                return resp

            if 200 <= resp.status_code < 300:
                return resp

            # Detect rate limits (429 or GitHub 403 primary/secondary limits)
            body_lower = (resp.text or "").lower()
            is_rate_limit = resp.status_code == 429 or (
                resp.status_code == 403
                and (
                    "rate limit" in body_lower
                    or "secondary rate" in body_lower
                    or "abuse detection" in body_lower
                )
            )
            if is_rate_limit:
                if attempt == self.max_retries:
                    resp.raise_for_status()
                with self._lock:
                    self.total_retries += 1
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait = float(retry_after) + random.uniform(0.5, 2.0)
                elif reset and reset.isdigit():
                    wait = max(1.0, float(reset) - time.time() + 2.0)
                else:
                    wait = min(self.max_delay, self.base_delay * (2**attempt) + random.uniform(1.0, 4.0))
                self._set_global_pause(wait, f"HTTP {resp.status_code} on {url}")
                continue

            if resp.status_code == 403 and allow_403:
                return resp

            if resp.status_code in (500, 502, 503, 504):
                if attempt == self.max_retries:
                    resp.raise_for_status()
                self._backoff(attempt, f"HTTP {resp.status_code} on {url}")
                continue

            resp.raise_for_status()

        return None

    def _backoff(self, attempt: int, reason: str) -> None:
        with self._lock:
            self.total_retries += 1
        delay = min(self.max_delay, self.base_delay * (2**attempt) + random.uniform(0.25, 1.5))
        print(f"\n[backoff] {reason}; retrying in {delay:.1f}s (attempt {attempt + 1}/{self.max_retries})")
        time.sleep(delay)