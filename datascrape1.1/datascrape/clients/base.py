from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional, Union


def hours_between(start_iso: Optional[str], end_iso: Optional[str]) -> Optional[float]:
    if not start_iso or not end_iso:
        return None
    try:
        s = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        return round((e - s).total_seconds() / 3600.0, 2)
    except Exception:
        return None


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return round(ordered[idx], 2)


class VCSClient(ABC):
    """Unified interface for GitHub Cloud, GHES, Bitbucket Cloud, and Bitbucket Server/DC."""

    def __init__(
        self,
        platform: str,
        deployment_type: str,
        base_url: str,
        token: str,
        org_or_project: str,
        verify_ssl: Union[bool, str] = True,
        auth_type: str = "bearer",
        max_retries: int = 6,
        timeout: int = 30,
        rate_buffer: int = 10,
        service_pattern: Optional[str] = None,
        custom_ci_files: Optional[List[str]] = None,
        enterprise: str = "",
    ):
        self.platform = platform
        self.deployment_type = deployment_type
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.org_or_project = org_or_project
        self.verify_ssl = verify_ssl
        self.auth_type = auth_type
        self.max_retries = max_retries
        self.timeout = timeout
        self.rate_buffer = rate_buffer
        self.service_pattern = service_pattern
        self.custom_ci_files = custom_ci_files or []
        self.enterprise = enterprise
        self.http: Any = None

    @abstractmethod
    def list_orgs(self) -> List[str]: ...

    @abstractmethod
    def list_repos(self) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def check_owner_and_ci(self, repo: Dict[str, Any]) -> Dict[str, Any]: ...

    @abstractmethod
    def list_apps_and_service_accounts(self) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def list_pull_requests(
        self, repo: Dict[str, Any], since: Optional[str] = None, until: Optional[str] = None
    ) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def audit_repo(self, repo: Dict[str, Any]) -> Dict[str, Any]: ...

    @abstractmethod
    def audit_repo_configurations(self, repo: Dict[str, Any]) -> Dict[str, Any]: ...

    @abstractmethod
    def raw_query(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        repo: Optional[Dict[str, Any]] = None,
        extract_key: Optional[str] = None,
    ) -> List[Dict[str, Any]]: ...

    # Extended reports with graceful defaults for cross-platform safety
    def list_languages(self, repo: Dict[str, Any]) -> Dict[str, Any]:
        return {"platform": self.platform, "org": self.org_or_project, "repo": repo["name"], "status": "NOT_SUPPORTED"}

    def list_releases_tags(self, repo: Dict[str, Any]) -> Dict[str, Any]:
        return {"platform": self.platform, "org": self.org_or_project, "repo": repo["name"], "status": "NOT_SUPPORTED"}

    def list_submodules(self, repo: Dict[str, Any]) -> List[Dict[str, Any]]:
        return []

    def list_teams_members(self) -> List[Dict[str, Any]]:
        return []

    def list_webhooks_deploy_keys(self, repo: Dict[str, Any]) -> List[Dict[str, Any]]:
        return []

    def list_iam_tokens_sso(self) -> List[Dict[str, Any]]:
        return []

    def list_dormant_users(self, inactive_days: int = 90) -> List[Dict[str, Any]]:
        return []

    def list_security_alerts(self) -> List[Dict[str, Any]]:
        return []

    def list_org_secrets_variables(self) -> List[Dict[str, Any]]:
        return []

    def audit_sbom_licenses(self, repo: Dict[str, Any]) -> Dict[str, Any]:
        return {"platform": self.platform, "org": self.org_or_project, "repo": repo["name"], "status": "NOT_SUPPORTED"}

    def audit_actions_pinning(self, repo: Dict[str, Any]) -> List[Dict[str, Any]]:
        return []

    def list_audit_log(self, since: Optional[str] = None, until: Optional[str] = None) -> List[Dict[str, Any]]:
        return []

    def list_runners_packages(self) -> List[Dict[str, Any]]:
        return []

    def list_environments(self, repo: Dict[str, Any]) -> List[Dict[str, Any]]:
        return []

    def audit_workflow_runs(self, repo: Dict[str, Any]) -> Dict[str, Any]:
        return {"platform": self.platform, "org": self.org_or_project, "repo": repo["name"], "status": "NOT_SUPPORTED"}

    def audit_pages(self, repo: Dict[str, Any]) -> Dict[str, Any]:
        return {"platform": self.platform, "org": self.org_or_project, "repo": repo["name"], "pages_enabled": False}

    def audit_pr_reviews(
        self, repo: Dict[str, Any], since: Optional[str] = None, until: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        return []

    def audit_contributors_bus_factor(self, repo: Dict[str, Any]) -> Dict[str, Any]:
        return {"platform": self.platform, "org": self.org_or_project, "repo": repo["name"], "status": "NOT_SUPPORTED"}

    def audit_issues_sla(
        self, repo: Dict[str, Any], since: Optional[str] = None, until: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        return []

    def audit_dora_metrics(
        self, repo: Dict[str, Any], since: Optional[str] = None, until: Optional[str] = None
    ) -> Dict[str, Any]:
        return {"platform": self.platform, "org": self.org_or_project, "repo": repo["name"], "status": "NOT_SUPPORTED"}

    def list_billing_copilot(self) -> List[Dict[str, Any]]:
        return []

    def audit_enterprise_policies(self) -> List[Dict[str, Any]]:
        """Return enterprise-level configurations and governance policies."""
        return []

    def graphql_query(self, query: str, variables: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        raise NotImplementedError(f"GraphQL is not supported on {self.platform}")