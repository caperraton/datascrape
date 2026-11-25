from __future__ import annotations

import threading
from datetime import datetime, timezone
from email.utils import format_datetime

import pytest
import requests

from datascrape.http_client import (
    RateLimitCoordinator,
    ResilientHTTPClient,
    _retry_after_seconds,
    _safe_url,
)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def request(self, **kwargs):
        self.calls += 1
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def response(status: int, headers: dict[str, str] | None = None, text: str = "") -> requests.Response:
    item = requests.Response()
    item.status_code = status
    item.headers.update(headers or {})
    item._content = text.encode("utf-8")
    item.url = "https://api.github.com/test"
    return item


def test_retry_after_accepts_seconds_and_http_date():
    assert _retry_after_seconds("15") == 15
    future = datetime.now(timezone.utc).replace(microsecond=0)
    future = future.replace(second=(future.second + 20) % 60)
    parsed = _retry_after_seconds(format_datetime(future, usegmt=True))
    assert parsed is not None
    assert parsed >= 0


def test_rate_limit_coordinator_is_shared_by_key():
    first = RateLimitCoordinator.shared("test-shared-key")
    second = RateLimitCoordinator.shared("test-shared-key")
    assert first is second
    first.pause_for(0.05, "test pause")
    assert second.remaining() > 0


def test_primary_rate_limit_uses_reset_timestamp(monkeypatch):
    client = ResilientHTTPClient(coordinator_key="test-primary-reset")
    monkeypatch.setattr("datascrape.http_client.time.time", lambda: 1000.0)
    item = response(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "4600"})
    delay, reason = client._rate_limit_delay(item, 0)
    assert delay == pytest.approx(3601.0)
    assert "primary" in reason


def test_secondary_rate_limit_waits_at_least_one_minute():
    client = ResilientHTTPClient(coordinator_key="test-secondary")
    item = response(403, {}, "You have exceeded a secondary rate limit")
    delay, reason = client._rate_limit_delay(item, 0)
    assert delay >= 60
    assert "secondary" in reason


def test_transient_http_error_retries(monkeypatch):
    client = ResilientHTTPClient(max_retries=2, coordinator_key="test-500")
    fake = FakeSession([response(503), response(200)])
    monkeypatch.setattr(client, "_get_session", lambda: fake)
    monkeypatch.setattr(client, "_exponential_delay", lambda attempt: 0.0)
    monkeypatch.setattr("datascrape.http_client.time.sleep", lambda seconds: None)
    result = client.request("GET", "https://api.github.com/test")
    assert result is not None and result.status_code == 200
    assert client.total_retries == 1
    assert fake.calls == 2


def test_network_error_retries(monkeypatch):
    client = ResilientHTTPClient(max_retries=2, coordinator_key="test-network")
    fake = FakeSession([requests.ConnectionError("offline"), response(200)])
    monkeypatch.setattr(client, "_get_session", lambda: fake)
    monkeypatch.setattr(client, "_exponential_delay", lambda attempt: 0.0)
    monkeypatch.setattr("datascrape.http_client.time.sleep", lambda seconds: None)
    result = client.request("GET", "https://api.github.com/test")
    assert result is not None and result.status_code == 200
    assert client.total_retries == 1


def test_rate_limited_response_retries_and_pauses_shared_workers(monkeypatch):
    client = ResilientHTTPClient(max_retries=2, coordinator_key="test-429")
    fake = FakeSession([response(429, {"Retry-After": "0.5"}), response(200)])
    monkeypatch.setattr(client, "_get_session", lambda: fake)
    monkeypatch.setattr(client.coordinator, "wait", lambda: None)
    monkeypatch.setattr("datascrape.http_client.random.uniform", lambda low, high: 0.0)
    result = client.request("GET", "https://api.github.com/test")
    assert result is not None and result.status_code == 200
    assert client.total_retries == 1
    assert client.coordinator.pause_count == 1


def test_sessions_are_thread_local():
    client = ResilientHTTPClient(coordinator_key="test-thread-local")
    barrier = threading.Barrier(3)
    sessions = []

    def get_session():
        sessions.append(client.session)
        barrier.wait()

    threads = [threading.Thread(target=get_session) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]


def test_shutdown_interrupts_rate_limit_wait(monkeypatch):
    from datascrape.shutdown import ShutdownRequested, clear_shutdown, request_shutdown

    client = ResilientHTTPClient(coordinator_key="test-shutdown")
    client.coordinator.pause_for(10, "test shutdown pause")
    request_shutdown()
    try:
        with pytest.raises(ShutdownRequested):
            client.coordinator.wait()
    finally:
        clear_shutdown()


def test_safe_url_removes_credentials_and_query_parameters():
    assert _safe_url("https://user:password@example.test/api?token=secret#fragment") == "https://example.test/api"
