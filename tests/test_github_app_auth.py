from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from datascrape.auth import GitHubAppTokenProvider


class FakeTokenResponse:
    ok = True
    status_code = 201
    headers = {}
    text = "{}"

    def json(self):
        expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        return {"token": "installation-token", "expires_at": expiry}


def test_github_app_token_is_cached_and_refreshed_through_provider():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    provider = GitHubAppTokenProvider(
        "https://api.github.com",
        "12345",
        "98765",
        pem,
    )
    calls = []

    def fake_post(url, headers, **kwargs):
        calls.append((url, headers))
        return FakeTokenResponse()

    provider._session.post = fake_post
    assert provider() == "installation-token"
    assert provider() == "installation-token"
    assert len(calls) == 1
    assert calls[0][0].endswith("/app/installations/98765/access_tokens")
    assert calls[0][1]["Authorization"].startswith("Bearer ")
    provider.close()


def test_client_factory_resolves_github_app_environment_values(tmp_path, monkeypatch):
    from datascrape.clients import build_client

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    key_path = tmp_path / "app.pem"
    key_path.write_text(pem)
    monkeypatch.setenv("TEST_APP_ID", "12345")
    monkeypatch.setenv("TEST_INSTALLATION_ID", "98765")
    monkeypatch.setenv("TEST_PRIVATE_KEY_FILE", str(key_path))
    client = build_client(
        "github",
        "cloud",
        "https://api.github.com",
        "",
        "acme",
        auth_type="github_app",
        app_id="ENV:TEST_APP_ID",
        installation_id="ENV:TEST_INSTALLATION_ID",
        private_key="ENV:TEST_PRIVATE_KEY_FILE",
    )
    assert client.app_token_provider.app_id == "12345"
    assert client.app_token_provider.installation_id == "98765"
    client.app_token_provider.close()
