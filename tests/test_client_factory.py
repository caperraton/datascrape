from __future__ import annotations

import pytest

from datascrape.clients import build_client


def test_github_rejects_basic_authentication():
    with pytest.raises(ValueError, match="basic auth is not supported"):
        build_client("github", "cloud", "https://api.github.com", "user:password", "acme", auth_type="basic")


def test_bitbucket_basic_auth_requires_username_and_password():
    with pytest.raises(ValueError, match="username:app_password"):
        build_client("bitbucket", "cloud", "https://api.bitbucket.org/2.0", "missing-separator", "workspace", auth_type="basic")
