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
    max_retries: int = 6,
    timeout: int = 30,
    rate_buffer: int = 10,
    service_pattern: Optional[str] = None,
    custom_ci_files: Optional[List[str]] = None,
    enterprise: str = "",
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
    )
    if t in ("github", "ghes"):
        dtype = "self_hosted" if t == "ghes" else deployment_type
        return GitHubClient(dtype, url, secret, org, **kwargs)
    if t in ("bitbucket", "bitbucket_cloud", "bitbucket_server"):
        dtype = "self_hosted" if t == "bitbucket_server" else deployment_type
        return BitbucketClient(dtype, url, secret, org, **kwargs)
    raise ValueError(f"Unsupported target '{target}'. Use github, ghes, or bitbucket.")