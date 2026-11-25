from __future__ import annotations

import requests

from datascrape.clients.github import GitHubClient


class FakeResponse:
    status_code = 200

    def json(self):
        return {
            "resources": {
                "core": {"limit": 5000, "remaining": 4900, "reset": 1900000000},
                "search": {"limit": 30, "remaining": 25, "reset": 1900000000},
            }
        }


class FakeHTTP:
    coordinator_key = "fake-rate-check"

    def __init__(self):
        self.urls = []

    def request(self, method, url, **kwargs):
        self.urls.append((method, url, kwargs))
        return FakeResponse()


def test_github_rate_limit_preflight_calls_rate_limit_endpoint():
    client = GitHubClient("cloud", "https://api.github.com", "token", "acme", rate_buffer=0)
    fake = FakeHTTP()
    client.http = fake
    state = client.check_rate_limit()
    assert fake.urls[0][0] == "GET"
    assert fake.urls[0][1] == "https://api.github.com/rate_limit"
    assert state["resources"]["core"]["remaining"] == 4900


def test_github_org_preflight_reports_visible_org():
    client = GitHubClient("cloud", "https://api.github.com", "token", "acme", rate_buffer=0)

    response = requests.Response()
    response.status_code = 200
    client.http = type("FakeOrgHTTP", (), {"request": lambda self, *args, **kwargs: response})()
    client.http.request = lambda *args, **kwargs: response
    assert client.check_org_access() is True
