import os
from typing import List, Optional, Union
from datascrape.clients.base import VCSClient
from datascrape.clients.bitbucket import BitbucketClient
from datascrape.clients.github import GitHubClient


def resolve_secret(value: str) -> str:
    """Resolve ENV:VAR_NAME tokens so secrets never live in config files or shell history."""
    if value and value.startswith("ENV:"):
        env_var = value[4:].strip()
        resolved = os.environ.get(env_var, "")
        if not resolved:
            raise ValueError(f"Environment variable '{env_var}' is not set.")
        return resolved
    return value


def build_client(
    target: str,
    deployment_type: str,
    url: str,
    token: str,
    org: str,
    verify_ssl: Union[bool, str] = True,
    auth_type: str = "bearer",
    max_retries: int = 8,
    timeout: int = 30,
    rate_buffer: int = 10,
    service_pattern: Optional[str] = None,
    custom_ci_files: Optional[List[str]] = None,
    enterprise: str = "",
    app_id: Optional[str] = None,
    installation_id: Optional[str] = None,
    private_key: Optional[str] = None,
    backoff_base: float = 2.0,
    backoff_max: float = 900.0,
) -> VCSClient:
    t = target.lower().strip()
    secret = resolve_secret(token)
    kwargs = dict(
        verify_ssl=verify_ssl,
        auth_type=auth_type,
        max_retries=max_retries,
        timeout=timeout,
        rate_buffer=rate_buffer,
        service_pattern=service_pattern,
        custom_ci_files=custom_ci_files,
        enterprise=enterprise,
        backoff_base=backoff_base,
        backoff_max=backoff_max,
    )
    if t in ("github", "ghes"):
        if auth_type == "basic":
            raise ValueError("GitHub and GHES require bearer PAT authentication or github_app authentication; basic auth is not supported.")
        dtype = "self_hosted" if t == "ghes" else deployment_type
        resolved_app_id = resolve_secret(app_id) if app_id else None
        resolved_installation_id = resolve_secret(installation_id) if installation_id else None
        resolved_private_key = resolve_secret(private_key) if private_key else None
        return GitHubClient(
            dtype,
            url,
            secret,
            org,
            app_id=resolved_app_id,
            installation_id=resolved_installation_id,
            private_key=resolved_private_key,
            **kwargs,
        )
    if t in ("bitbucket", "bitbucket_cloud", "bitbucket_server"):
        if auth_type == "github_app":
            raise ValueError("GitHub App authentication is only supported for GitHub and GHES targets.")
        dtype = "self_hosted" if t == "bitbucket_server" else deployment_type
        return BitbucketClient(dtype, url, secret, org, **kwargs)
    raise ValueError(f"Unsupported target '{target}'. Use github, ghes, or bitbucket.")