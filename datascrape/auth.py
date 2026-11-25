"""Authentication helpers for GitHub App installation tokens."""
from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import threading
import time
from pathlib import Path
from typing import Optional, Union

import requests

from datascrape.shutdown import ShutdownRequested, wait_or_shutdown

logger = logging.getLogger(__name__)


class GitHubAppTokenProvider:
    """Create and cache short-lived GitHub App installation access tokens."""

    def __init__(
        self,
        api_base_url: str,
        app_id: str,
        installation_id: str,
        private_key: Union[str, Path],
        verify_ssl: Union[bool, str] = True,
        timeout: int = 30,
    ) -> None:
        try:
            import jwt  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise RuntimeError("GitHub App authentication requires PyJWT. Install datascrape with its standard dependencies.") from exc

        self._jwt = jwt
        self.api_base_url = api_base_url.rstrip("/")
        self.app_id = str(app_id)
        self.installation_id = str(installation_id)
        key_value = str(private_key)
        key_path = Path(key_value).expanduser()
        if "-----BEGIN" not in key_value and key_path.is_file():
            self.private_key = key_path.read_text(encoding="utf-8")
        else:
            self.private_key = key_value.replace("\\n", "\n")
        if "-----BEGIN" not in self.private_key:
            raise ValueError("GitHub App private key must be a PEM value or a path to a PEM file.")
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires_at = 0.0
        self._session = requests.Session()

    def __call__(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at - 120:
                return self._token
            now = int(time.time())
            assertion = self._jwt.encode(
                {"iat": now - 60, "exp": now + 540, "iss": self.app_id},
                self.private_key,
                algorithm="RS256",
            )
            url = f"{self.api_base_url}/app/installations/{self.installation_id}/access_tokens"
            response = None
            for attempt in range(6):
                try:
                    response = self._session.post(
                        url,
                        headers={
                            "Authorization": f"Bearer {assertion}",
                            "Accept": "application/vnd.github+json",
                            "X-GitHub-Api-Version": "2022-11-28",
                        },
                        timeout=self.timeout,
                        verify=self.verify_ssl,
                    )
                except (requests.Timeout, requests.ConnectionError, requests.ChunkedEncodingError):
                    if attempt == 5:
                        raise
                    delay = min(60.0, 2.0 * (2**attempt)) + random.uniform(0.0, 1.0)
                    logger.warning("GitHub App token endpoint network error; retry %s/5 in %.1fs", attempt + 1, delay)
                    if wait_or_shutdown(delay):
                        raise ShutdownRequested("Shutdown requested during GitHub App token refresh backoff")
                    continue
                body = (response.text or "").lower()
                retryable = response.status_code in (408, 425, 429, 500, 502, 503, 504) or (
                    response.status_code == 403 and ("rate limit" in body or "secondary rate" in body)
                )
                if not retryable or attempt == 5:
                    break
                retry_after = response.headers.get("Retry-After")
                delay = None
                if retry_after:
                    try:
                        delay = max(0.0, float(retry_after))
                    except ValueError:
                        try:
                            retry_at = parsedate_to_datetime(retry_after)
                            if retry_at.tzinfo is None:
                                retry_at = retry_at.replace(tzinfo=timezone.utc)
                            delay = max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
                        except (TypeError, ValueError, OverflowError):
                            delay = None
                if delay is None:
                    remaining = response.headers.get("X-RateLimit-Remaining")
                    reset = response.headers.get("X-RateLimit-Reset")
                    if remaining == "0" and reset:
                        try:
                            delay = max(1.0, float(reset) - time.time() + 1.0)
                        except ValueError:
                            delay = None
                if delay is None:
                    delay = min(60.0, 2.0 * (2**attempt))
                    if "secondary rate limit" in body or "abuse detection" in body:
                        delay = max(60.0, delay)
                delay += random.uniform(0.0, 1.0)
                logger.warning("GitHub App token endpoint returned HTTP %s; retry %s/5 in %.1fs", response.status_code, attempt + 1, delay)
                if wait_or_shutdown(delay):
                    raise ShutdownRequested("Shutdown requested during GitHub App token refresh backoff")
            if response is None:
                raise RuntimeError("GitHub App token endpoint returned no response.")
            if not response.ok:
                logger.error("GitHub App token refresh failed with HTTP %s", response.status_code)
                response.raise_for_status()
            payload = response.json()
            token = payload.get("token")
            expires_at = payload.get("expires_at")
            if not token or not expires_at:
                raise RuntimeError("GitHub did not return an installation token and expiry timestamp.")
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp()
            self._token = str(token)
            self._expires_at = expiry
            logger.info("GitHub App installation token refreshed; expires at %s", expires_at)
            return self._token

    def close(self) -> None:
        self._session.close()
