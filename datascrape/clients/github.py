import base64
import hashlib
import logging
from datetime import datetime, timezone
import re
import time
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import quote
from datascrape.auth import GitHubAppTokenProvider
from datascrape.clients.base import VCSClient, hours_between, percentile
from datascrape.http_client import ResilientHTTPClient

logger = logging.getLogger(__name__)

DEFAULT_SERVICE_PATTERN = (
    r"(bot|svc|srv|service|sa_|sa-|ci-|cd-|deploy|automation|machine|pipeline|jenkins|github-actions)"
)
DEFAULT_CI_FILES: List[Tuple[str, str]] = [
    (".github/workflows", "github_actions"),
    ("Jenkinsfile", "jenkins"),
    ("azure-pipelines.yml", "azure_pipelines"),
    (".circleci/config.yml", "circleci"),
    (".gitlab-ci.yml", "gitlab_ci"),
    ("bitbucket-pipelines.yml", "bitbucket_pipelines"),
    (".travis.yml", "travis_ci"),
]
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
USES_RE = re.compile(r"^\s*-?\s*uses:\s*['\"]?([^'\"#\s]+)['\"]?", re.MULTILINE)
SUBMODULE_URL_RE = re.compile(r"^\s*url\s*=\s*(.+)$", re.MULTILINE)
SUBMODULE_PATH_RE = re.compile(r"^\s*path\s*=\s*(.+)$", re.MULTILINE)


class GitHubClient(VCSClient):
    def __init__(
        self,
        deployment_type: str,
        base_url: str,
        token: str,
        org_or_project: str,
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
    ):
        clean_url = (base_url or "https://api.github.com").rstrip("/")
        if deployment_type == "cloud":
            api_url = "https://api.github.com"
            self.graphql_url = "https://api.github.com/graphql"
        else:
            server_root = clean_url[:-7] if clean_url.endswith("/api/v3") else clean_url
            api_url = f"{server_root}/api/v3"
            self.graphql_url = f"{server_root}/api/graphql"

        super().__init__(
            "github",
            deployment_type,
            api_url,
            token,
            org_or_project,
            verify_ssl,
            auth_type,
            max_retries,
            timeout,
            rate_buffer,
            service_pattern,
            custom_ci_files,
            enterprise=enterprise,
        )
        self.service_regex = re.compile(service_pattern or DEFAULT_SERVICE_PATTERN, re.IGNORECASE)
        self.ci_files = list(DEFAULT_CI_FILES) + [(p, f"custom:{p}") for p in self.custom_ci_files]
        self.app_token_provider = None
        if auth_type == "github_app" and not (app_id and installation_id and private_key):
            raise ValueError("auth_type='github_app' requires app_id, installation_id, and private_key.")
        if app_id or installation_id or private_key:
            if not (app_id and installation_id and private_key):
                raise ValueError("GitHub App authentication requires app_id, installation_id, and private_key.")
            self.app_token_provider = GitHubAppTokenProvider(
                api_base_url=api_url,
                app_id=app_id,
                installation_id=installation_id,
                private_key=private_key,
                verify_ssl=verify_ssl,
                timeout=timeout,
            )
        identity = (
            f"github-app:{app_id}:{installation_id}"
            if self.app_token_provider
            else f"github-token:{token}"
        )
        coordinator_key = f"github:{api_url}:{hashlib.sha256(identity.encode()).hexdigest()}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if not self.app_token_provider:
            headers["Authorization"] = f"Bearer {token}"
        self.http = ResilientHTTPClient(
            headers=headers,
            max_retries=max_retries,
            base_delay=backoff_base,
            max_delay=backoff_max,
            timeout=timeout,
            verify_ssl=verify_ssl,
            min_rate_buffer=rate_buffer,
            coordinator_key=coordinator_key,
            token_provider=self.app_token_provider,
        )

    def close(self) -> None:
        super().close()
        if self.app_token_provider is not None:
            self.app_token_provider.close()

    def check_rate_limit(self) -> Dict[str, Any]:
        """Read GitHub's live rate-limit resources before a scan starts."""
        response = self.http.request("GET", f"{self.base_url}/rate_limit")
        if response is None:
            raise RuntimeError("GitHub rate-limit endpoint returned no response.")
        payload = response.json()
        resources = payload.get("resources", {})
        for name, resource in resources.items():
            remaining = resource.get("remaining")
            reset = resource.get("reset")
            logger.info(
                "GitHub rate limit %-24s remaining=%s/%s reset=%s",
                name,
                remaining if remaining is not None else "?",
                resource.get("limit", "?"),
                reset if reset is not None else "?",
            )
            # Repository and organization discovery use the core REST quota.
            # Other resource pools are independent; merely observing a small or
            # exhausted specialized quota must not block unrelated requests.
            # The HTTP transport still handles throttling on actual requests.
            if name != "core" or remaining is None or reset is None:
                continue
            try:
                remaining_count = int(remaining)
                limit = int(resource["limit"]) if resource.get("limit") is not None else None
                reset_epoch = float(reset)
            except (TypeError, ValueError):
                logger.debug("Ignoring invalid GitHub rate-limit metadata for %s", name)
                continue
            if limit is not None and limit > 0 and remaining_count >= limit:
                continue
            if remaining_count <= self.rate_buffer:
                wait = max(0.0, reset_epoch - time.time() + 1.0)
                if wait > 0:
                    logger.warning(
                        "GitHub %s quota is low (%s remaining); shared workers will wait for reset at %s",
                        name,
                        remaining,
                        reset,
                    )
                    self.http.coordinator.pause_for(wait, f"{name} quota low: {remaining} remaining; reset at {reset}")
        return payload

    def check_org_access(self) -> bool:
        """Perform a lightweight organization access check without enumerating repositories."""
        if not self.org_or_project or self.org_or_project.lower() == "site":
            return True
        org_name = quote(self.org_or_project, safe="")
        response = self.http.request(
            "GET",
            f"{self.base_url}/orgs/{org_name}",
            allow_403=True,
            allow_404=True,
        )
        if response is None:
            logger.warning("Organization %s was not found or is not visible to this credential", self.org_or_project)
            return False
        if response.status_code == 403:
            logger.warning(
                "Credential can reach GitHub but cannot inspect organization %s; check organization membership and token/app permissions",
                self.org_or_project,
            )
            return False
        logger.info("Organization access preflight passed for %s", self.org_or_project)
        return True

    # ---------- Internal HTTP Helpers ----------

    def _paginate(self, url: str, params: Optional[Dict[str, Any]] = None, max_pages: Optional[int] = None) -> List[Any]:
        results: List[Any] = []
        next_url: Optional[str] = url
        next_params = dict(params or {})
        pages = 0
        while next_url:
            resp = self.http.request("GET", next_url, params=next_params)
            if not resp:
                break
            data = resp.json()
            if isinstance(data, list):
                results.extend(data)
            elif isinstance(data, dict):
                results.append(data)
            pages += 1
            if max_pages and pages >= max_pages:
                break
            next_url = resp.links.get("next", {}).get("url")
            next_params = {}
        return results

    def _safe_get_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        resp = self.http.request("GET", url, params=params, allow_404=True, allow_403=True, allow_422=True)
        if resp is None:
            return None
        if resp.status_code == 403:
            return "ACCESS_DENIED"
        return resp.json() if resp.text.strip() else {}

    def _safe_paginate(self, url: str, params: Optional[Dict[str, Any]] = None, max_pages: Optional[int] = None) -> Any:
        try:
            return self._paginate(url, params=params, max_pages=max_pages)
        except Exception as exc:
            if any(code in str(exc) for code in ("403", "404", "422")):
                return "ACCESS_DENIED"
            raise

    def _safe_graphql(self, query: str, variables: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
        """Run a GraphQL query without crashing on schema/permission differences across GHES versions."""
        try:
            resp = self.http.request(
                "POST",
                self.graphql_url,
                json_body={"query": query, "variables": variables},
                allow_403=True,
                allow_404=True,
            )
            if resp is None:
                return None, "NOT_AVAILABLE"
            if resp.status_code == 403:
                return None, "ACCESS_DENIED"
            payload = resp.json()
            errs = payload.get("errors") or []
            err_msg = "; ".join(e.get("message", str(e)) for e in errs)
            return payload.get("data"), err_msg
        except Exception as exc:
            return None, str(exc)

    def _path_exists(self, full_name: str, path: str) -> bool:
        resp = self.http.request(
            "GET", f"{self.base_url}/repos/{full_name}/contents/{path}", allow_404=True, allow_403=True
        )
        return resp is not None and resp.status_code == 200

    def _read_file_text(self, full_name: str, path: str) -> str:
        data = self._safe_get_json(f"{self.base_url}/repos/{full_name}/contents/{path}")
        if isinstance(data, dict) and data.get("content"):
            try:
                return base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
            except Exception:
                return ""
        return ""

    # ---------- Discovery ----------

    def list_orgs(self) -> List[str]:
        orgs = self._safe_paginate(f"{self.base_url}/user/orgs", {"per_page": 100})
        names = [o["login"] for o in orgs if isinstance(o, dict) and o.get("login")] if isinstance(orgs, list) else []
        if self.deployment_type == "self_hosted":
            all_orgs = self._safe_paginate(f"{self.base_url}/organizations", {"per_page": 100})
            if isinstance(all_orgs, list):
                names.extend(o["login"] for o in all_orgs if isinstance(o, dict) and o.get("login"))
        return sorted(set(names))

    def list_repos(self) -> List[Dict[str, Any]]:
        raw = self._paginate(f"{self.base_url}/orgs/{self.org_or_project}/repos", {"type": "all", "per_page": 100})
        return [
            {
                "platform": self.platform,
                "deployment_type": self.deployment_type,
                "org": self.org_or_project,
                "name": r["name"],
                "full_name": r["full_name"],
                "visibility": r.get("visibility", ""),
                "is_fork": bool(r.get("fork")),
                "is_template": bool(r.get("is_template")),
                "primary_language": r.get("language") or "",
                "default_branch": r.get("default_branch", "main"),
                "archived": bool(r.get("archived")),
                "disabled": bool(r.get("disabled")),
                "pushed_at": r.get("pushed_at"),
                "updated_at": r.get("updated_at"),
                "size_kb": int(r.get("size") or 0),
                "open_issues_count": int(r.get("open_issues_count") or 0),
                "html_url": r.get("html_url", ""),
                "_raw": r,
            }
            for r in raw
        ]

    # ---------- Core Assessment Reports ----------

    def check_owner_and_ci(self, repo: Dict[str, Any]) -> Dict[str, Any]:
        fn = repo["full_name"]
        has_codeowners = any(
            self._path_exists(fn, p) for p in ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS")
        )
        ci_types = [label for path, label in self.ci_files if self._path_exists(fn, path)]
        custom_props = self._safe_get_json(f"{self.base_url}/repos/{fn}/properties/values")
        prop_owner = ""
        if isinstance(custom_props, list):
            for p in custom_props:
                if isinstance(p, dict) and "owner" in str(p.get("property_name", "")).lower():
                    prop_owner = str(p.get("value") or "")
                    break
        return {
            "has_codeowners": has_codeowners,
            "custom_property_owner": prop_owner,
            "has_owner_signal": bool(has_codeowners or prop_owner),
            "has_ci": bool(ci_types),
            "ci_type": ";".join(ci_types) if ci_types else "none",
        }

    def list_apps_and_service_accounts(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []

        # Org owners as fallback escalation contacts for unowned apps/service accounts
        org_admins_raw = self._safe_paginate(
            f"{self.base_url}/orgs/{self.org_or_project}/members",
            {"role": "admin", "per_page": 100},
        )
        org_admins = (
            ";".join(u.get("login", "") for u in org_admins_raw if isinstance(u, dict) and u.get("login"))
            if isinstance(org_admins_raw, list)
            else ""
        )

        # Best-effort audit-log lookup for who installed each app (requires read:audit_log)
        installer_map: Dict[str, str] = {}
        audit = self._safe_paginate(
            f"{self.base_url}/orgs/{self.org_or_project}/audit-log",
            {"phrase": "action:integration_installation.create", "per_page": 100},
            max_pages=3,
        )
        if isinstance(audit, list):
            for ev in audit:
                if isinstance(ev, dict):
                    slug = ev.get("name") or ev.get("integration") or ""
                    actor = ev.get("actor") or ""
                    if slug and actor and slug not in installer_map:
                        installer_map[slug] = actor

        # 1. Installed GitHub Apps + registered owners + permissions
        pages = self._safe_paginate(f"{self.base_url}/orgs/{self.org_or_project}/installations", {"per_page": 100})
        if isinstance(pages, list):
            for page in pages:
                for inst in (page.get("installations", []) if isinstance(page, dict) else []):
                    slug = inst.get("app_slug") or ""
                    app_meta = self._safe_get_json(f"{self.base_url}/apps/{slug}") if slug else None
                    reg_owner = ""
                    reg_owner_type = ""
                    app_url = ""
                    if isinstance(app_meta, dict) and isinstance(app_meta.get("owner"), dict):
                        reg_owner = app_meta["owner"].get("login", "")
                        reg_owner_type = app_meta["owner"].get("type", "")
                        app_url = app_meta.get("external_url") or app_meta.get("html_url", "")
                    elif app_meta is None:
                        reg_owner = "PRIVATE_OR_INTERNAL_APP"

                    perms = inst.get("permissions") or {}
                    perms_str = ";".join(f"{k}:{v}" for k, v in sorted(perms.items()))
                    events_str = ";".join(inst.get("events") or [])

                    rows.append({
                        "platform": self.platform,
                        "org": self.org_or_project,
                        "record_type": "github_app",
                        "identifier": slug or str(inst.get("app_id")),
                        "app_id": inst.get("app_id", ""),
                        "registered_app_owner": reg_owner,
                        "registered_owner_type": reg_owner_type,
                        "installed_by_audit_log": installer_map.get(slug, ""),
                        "org_admins_fallback": org_admins,
                        "scope": inst.get("repository_selection", ""),
                        "permissions": perms_str,
                        "events": events_str,
                        "suspended": bool(inst.get("suspended_at")),
                        "created_at": inst.get("created_at", ""),
                        "updated_at": inst.get("updated_at", ""),
                        "app_url": app_url,
                        "business_owner": "",
                    })
        else:
            rows.append({
                "platform": self.platform,
                "org": self.org_or_project,
                "record_type": "github_app",
                "identifier": "ACCESS_DENIED_REQUIRES_ORG_OWNER",
                "app_id": "",
                "registered_app_owner": "",
                "registered_owner_type": "",
                "installed_by_audit_log": "",
                "org_admins_fallback": org_admins,
                "scope": "",
                "permissions": "",
                "events": "",
                "suspended": "",
                "created_at": "",
                "updated_at": "",
                "app_url": "",
                "business_owner": "",
            })

        # 2. Candidate Service / Machine Accounts
        for endpoint, source_type in (("members", "org_member"), ("outside_collaborators", "outside_collaborator")):
            users = self._safe_paginate(f"{self.base_url}/orgs/{self.org_or_project}/{endpoint}", {"per_page": 100})
            if isinstance(users, list):
                for u in users:
                    login = u.get("login", "")
                    if self.service_regex.search(login) or u.get("type") == "Bot":
                        rows.append({
                            "platform": self.platform,
                            "org": self.org_or_project,
                            "record_type": f"service_account_candidate:{source_type}",
                            "identifier": login,
                            "app_id": u.get("id", ""),
                            "registered_app_owner": "",
                            "registered_owner_type": u.get("type", ""),
                            "installed_by_audit_log": "",
                            "org_admins_fallback": org_admins,
                            "scope": u.get("type", ""),
                            "permissions": "",
                            "events": "",
                            "suspended": "",
                            "created_at": "",
                            "updated_at": "",
                            "app_url": u.get("html_url", ""),
                            "business_owner": "",
                        })
        return rows

    def _detect_lfs(self, full_name: str) -> bool:
        if "filter=lfs" in self._read_file_text(full_name, ".gitattributes"):
            return True
        return self._path_exists(full_name, ".lfsconfig")

    def audit_repo(self, repo: Dict[str, Any]) -> Dict[str, Any]:
        fn = repo["full_name"]
        base = f"{self.base_url}/repos/{fn}"

        lfs_enabled = self._detect_lfs(fn)
        act_perm = self._safe_get_json(f"{base}/actions/permissions")
        act_wf_perm = self._safe_get_json(f"{base}/actions/permissions/workflow")
        wf_pages = self._safe_paginate(f"{base}/actions/workflows", {"per_page": 100})
        workflows = []
        if isinstance(wf_pages, list):
            for p in wf_pages:
                workflows.extend(p.get("workflows", []) if isinstance(p, dict) else [])

        sec_pages = self._safe_paginate(f"{base}/actions/secrets", {"per_page": 100})
        if sec_pages == "ACCESS_DENIED":
            secret_names: Any = "ACCESS_DENIED"
            secret_count: Any = "ACCESS_DENIED"
        else:
            names = [s.get("name", "") for p in sec_pages if isinstance(p, dict) for s in p.get("secrets", [])]
            secret_names, secret_count = ";".join(names), len(names)

        prot_branches = self._safe_paginate(f"{base}/branches", {"protected": "true", "per_page": 100})
        prot_names = [b["name"] for b in prot_branches if isinstance(b, dict)] if isinstance(prot_branches, list) else []

        rulesets = self._safe_paginate(f"{base}/rulesets", {"includes_parents": "true", "per_page": 100})
        ruleset_names = (
            [f"{rs.get('name')}({rs.get('enforcement')})" for rs in rulesets if isinstance(rs, dict)]
            if isinstance(rulesets, list)
            else []
        )

        teams_raw = self._safe_paginate(f"{base}/teams", {"per_page": 100})
        if isinstance(teams_raw, list):
            teams_list = [f"{t.get('slug')}:{t.get('permission')}" for t in teams_raw if isinstance(t, dict)]
            admin_teams = [t.get("slug") for t in teams_raw if isinstance(t, dict) and (t.get("permissions") or {}).get("admin")]
        else:
            teams_list, admin_teams = ["ACCESS_DENIED"], []

        direct_raw = self._safe_paginate(f"{base}/collaborators", {"affiliation": "direct", "per_page": 100})
        direct_list = (
            [f"{u.get('login')}:{u.get('role_name', '')}" for u in direct_raw if isinstance(u, dict)]
            if isinstance(direct_raw, list)
            else ["ACCESS_DENIED"]
        )

        outside_raw = self._safe_paginate(f"{base}/collaborators", {"affiliation": "outside", "per_page": 100})
        outside_list = (
            [f"{u.get('login')}:{u.get('role_name', '')}" for u in outside_raw if isinstance(u, dict)]
            if isinstance(outside_raw, list)
            else ["ACCESS_DENIED"]
        )

        admins_raw = self._safe_paginate(
            f"{base}/collaborators", {"affiliation": "all", "permission": "admin", "per_page": 100}
        )
        admin_users = (
            [u.get("login", "") for u in admins_raw if isinstance(u, dict)]
            if isinstance(admins_raw, list)
            else ["ACCESS_DENIED"]
        )

        return {
            "platform": self.platform,
            "org": self.org_or_project,
            "repo": repo["name"],
            "full_name": fn,
            "visibility": repo.get("visibility", ""),
            "archived": repo["archived"],
            "size_kb": repo["size_kb"],
            "size_mb": round(repo["size_kb"] / 1024, 2),
            "lfs_enabled": lfs_enabled,
            "actions_enabled": act_perm.get("enabled") if isinstance(act_perm, dict) else act_perm,
            "actions_allowed_policy": act_perm.get("allowed_actions") if isinstance(act_perm, dict) else "",
            "actions_default_token_perm": act_wf_perm.get("default_workflow_permissions") if isinstance(act_wf_perm, dict) else "",
            "actions_workflow_count": len(workflows),
            "repo_secrets_count": secret_count,
            "repo_secret_names": secret_names,
            "protected_branches": ";".join(prot_names),
            "rulesets": ";".join(ruleset_names),
            "has_branch_protection_or_ruleset": bool(prot_names or ruleset_names),
            "teams_count": len(teams_list) if teams_list != ["ACCESS_DENIED"] else "ACCESS_DENIED",
            "teams": ";".join(teams_list),
            "direct_assignments_count": len(direct_list) if direct_list != ["ACCESS_DENIED"] else "ACCESS_DENIED",
            "direct_assignments": ";".join(direct_list),
            "outside_collaborators": ";".join(outside_list),
            "admin_users": ";".join(admin_users),
            "admin_teams": ";".join(admin_teams),
            "html_url": repo["html_url"],
        }

    def audit_repo_configurations(self, repo: Dict[str, Any]) -> Dict[str, Any]:
        fn = repo["full_name"]
        base = f"{self.base_url}/repos/{fn}"
        raw = repo.get("_raw") or self._safe_get_json(base) or {}
        if not isinstance(raw, dict):
            raw = {}
        sec = raw.get("security_and_analysis") or {}

        def _sec_status(key: str) -> str:
            return (sec.get(key) or {}).get("status") or "not_configured_or_no_access"

        default_branch = raw.get("default_branch") or "main"
        bp = self._safe_get_json(f"{base}/branches/{quote(default_branch, safe='')}/protection")
        has_default_bp = isinstance(bp, dict) and "url" in bp

        vuln_resp = self.http.request("GET", f"{base}/vulnerability-alerts", allow_404=True, allow_403=True)
        dependabot_alerts = vuln_resp is not None and vuln_resp.status_code == 204

        hooks = self._safe_paginate(f"{base}/hooks", {"per_page": 100})
        keys = self._safe_paginate(f"{base}/keys", {"per_page": 100})

        checks = {
            "has_readme": self._path_exists(fn, "README.md"),
            "has_codeowners": any(self._path_exists(fn, p) for p in ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS")),
            "has_security_md": any(self._path_exists(fn, p) for p in ("SECURITY.md", ".github/SECURITY.md")),
            "has_dependabot_yml": self._path_exists(fn, ".github/dependabot.yml"),
            "has_workflows_dir": self._path_exists(fn, ".github/workflows"),
        }
        present = [k.replace("has_", "") for k, v in checks.items() if v]
        missing = [k.replace("has_", "") for k, v in checks.items() if not v]
        (present if has_default_bp else missing).append("default_branch_protection")
        (present if dependabot_alerts else missing).append("dependabot_alerts")
        (present if _sec_status("secret_scanning") == "enabled" else missing).append("secret_scanning")

        return {
            "platform": self.platform,
            "org": self.org_or_project,
            "repo": repo["name"],
            "full_name": fn,
            "visibility": raw.get("visibility", ""),
            "default_branch": default_branch,
            "archived": repo["archived"],
            "has_issues": bool(raw.get("has_issues")),
            "has_wiki": bool(raw.get("has_wiki")),
            "has_projects": bool(raw.get("has_projects")),
            "has_discussions": bool(raw.get("has_discussions")),
            "allow_squash_merge": raw.get("allow_squash_merge"),
            "allow_merge_commit": raw.get("allow_merge_commit"),
            "allow_rebase_merge": raw.get("allow_rebase_merge"),
            "delete_branch_on_merge": raw.get("delete_branch_on_merge"),
            "allow_auto_merge": raw.get("allow_auto_merge"),
            "web_commit_signoff_required": raw.get("web_commit_signoff_required"),
            "advanced_security": _sec_status("advanced_security"),
            "secret_scanning": _sec_status("secret_scanning"),
            "secret_scanning_push_protection": _sec_status("secret_scanning_push_protection"),
            "dependabot_security_updates": _sec_status("dependabot_security_updates"),
            "dependabot_alerts_enabled": dependabot_alerts,
            "default_branch_protected": has_default_bp,
            "webhooks_count": len(hooks) if isinstance(hooks, list) else hooks,
            "deploy_keys_count": len(keys) if isinstance(keys, list) else keys,
            **checks,
            "configs_present": ";".join(present),
            "configs_missing": ";".join(missing),
        }

    # ---------- Enterprise-Layer Policies & Configurations ----------

    def audit_enterprise_policies(self) -> List[Dict[str, Any]]:
        ent = self.enterprise or self.org_or_project
        rows: List[Dict[str, Any]] = []

        def _add(category: str, name: str, value: Any, source_api: str, details: str = "") -> None:
            if value == "ACCESS_DENIED":
                status = "ACCESS_DENIED"
            elif value is None or value == "":
                status = "NOT_CONFIGURED"
            elif str(value).upper() == "NO_POLICY":
                status = "NO_POLICY_DELEGATED_TO_ORGS"
            else:
                status = "CONFIGURED"
            rows.append({
                "platform": self.platform,
                "deployment_type": self.deployment_type,
                "enterprise": ent,
                "category": category,
                "policy_name": name,
                "current_value": value if value is not None else "",
                "status": status,
                "source_api": source_api,
                "details": details,
            })

        # 1. GraphQL EnterpriseOwnerInfo split into blocks so older GHES schemas never fail the full report
        gql_blocks = [
            (
                "member_and_repo_policies",
                """
                query($slug: String!) {
                  enterprise(slug: $slug) {
                    name
                    slug
                    ownerInfo {
                      defaultRepositoryPermissionSetting
                      allowPrivateRepositoryForkingSetting
                      membersCanCreateRepositoriesSetting
                      membersCanCreatePublicRepositoriesSetting
                      membersCanCreatePrivateRepositoriesSetting
                      membersCanCreateInternalRepositoriesSetting
                      membersCanChangeRepositoryVisibilitySetting
                      membersCanDeleteRepositoriesSetting
                      membersCanDeleteIssuesSetting
                      membersCanInviteCollaboratorsSetting
                      membersCanMakePurchasesSetting
                      membersCanUpdateProtectedBranchesSetting
                      membersCanViewDependencyInsightsSetting
                    }
                  }
                }
                """,
            ),
            (
                "projects_and_collaboration",
                """
                query($slug: String!) {
                  enterprise(slug: $slug) {
                    ownerInfo {
                      organizationProjectsSetting
                      repositoryProjectsSetting
                      teamDiscussionsSetting
                      notificationDeliveryRestrictionEnabledSetting
                    }
                  }
                }
                """,
            ),
            (
                "authentication_and_network",
                """
                query($slug: String!) {
                  enterprise(slug: $slug) {
                    ownerInfo {
                      twoFactorRequiredSetting
                      affiliatedUsersWithTwoFactorDisabledExist
                      ipAllowListEnabledSetting
                      ipAllowListForInstalledAppsEnabledSetting
                      ipAllowListEntries(first: 100) { totalCount }
                      samlIdentityProvider {
                        ssoUrl
                        issuer
                        digestMethod
                        signatureMethod
                      }
                    }
                  }
                }
                """,
            ),
        ]

        if ent and ent != "site":
            for category, query in gql_blocks:
                data, err = self._safe_graphql(query, {"slug": ent})
                ent_obj = (data or {}).get("enterprise") or {}
                owner_info = ent_obj.get("ownerInfo")
                if not owner_info:
                    _add(
                        category,
                        f"graphql_{category}",
                        "ACCESS_DENIED" if "403" in err or "ACCESS_DENIED" in err else "",
                        "graphql:enterprise.ownerInfo",
                        details=err or "Requires enterprise owner / admin:enterprise scope",
                    )
                    continue
                for k, v in owner_info.items():
                    if k == "samlIdentityProvider":
                        if isinstance(v, dict):
                            _add(category, "saml_sso_configured", True, "graphql:ownerInfo", details=f"ssoUrl={v.get('ssoUrl')}; issuer={v.get('issuer')}")
                        else:
                            _add(category, "saml_sso_configured", False, "graphql:ownerInfo", details="No enterprise SAML IdP configured")
                    elif k == "ipAllowListEntries" and isinstance(v, dict):
                        _add(category, "ip_allow_list_entry_count", v.get("totalCount", 0), "graphql:ownerInfo")
                    else:
                        _add(category, k, v, "graphql:ownerInfo")

            # 2. REST Enterprise Actions Policies & Runner Groups
            act_perm = self._safe_get_json(f"{self.base_url}/enterprises/{ent}/actions/permissions")
            if isinstance(act_perm, dict):
                _add("actions_policies", "enabled_organizations", act_perm.get("enabled_organizations"), "rest:actions/permissions")
                _add("actions_policies", "allowed_actions", act_perm.get("allowed_actions"), "rest:actions/permissions")
            else:
                _add("actions_policies", "actions_permissions", act_perm, "rest:actions/permissions")

            act_sel = self._safe_get_json(f"{self.base_url}/enterprises/{ent}/actions/permissions/selected-actions")
            if isinstance(act_sel, dict):
                _add("actions_policies", "github_owned_allowed", act_sel.get("github_owned_allowed"), "rest:actions/selected-actions")
                _add("actions_policies", "verified_allowed", act_sel.get("verified_allowed"), "rest:actions/selected-actions")
                _add("actions_policies", "patterns_allowed", ";".join(act_sel.get("patterns_allowed") or []), "rest:actions/selected-actions")

            act_wf = self._safe_get_json(f"{self.base_url}/enterprises/{ent}/actions/permissions/workflow")
            if isinstance(act_wf, dict):
                _add("actions_policies", "default_workflow_permissions", act_wf.get("default_workflow_permissions"), "rest:actions/workflow")
                _add("actions_policies", "can_approve_pull_request_reviews", act_wf.get("can_approve_pull_request_reviews"), "rest:actions/workflow")

            rg = self._safe_get_json(f"{self.base_url}/enterprises/{ent}/actions/runner-groups")
            if isinstance(rg, dict) and "runner_groups" in rg:
                groups = rg["runner_groups"]
                summary = "; ".join(f"{g.get('name')}({g.get('visibility')})" for g in groups)
                _add("actions_policies", "runner_groups_count", len(groups), "rest:actions/runner-groups", details=summary)

            # 3. Enterprise Rulesets & Code Security Configurations
            rulesets = self._safe_paginate(f"{self.base_url}/enterprises/{ent}/rulesets", {"per_page": 100})
            if isinstance(rulesets, list):
                summary = "; ".join(f"{r.get('name')}({r.get('enforcement')}/{r.get('target')})" for r in rulesets if isinstance(r, dict))
                _add("governance_rulesets", "enterprise_rulesets_count", len(rulesets), "rest:enterprise/rulesets", details=summary)
            else:
                _add("governance_rulesets", "enterprise_rulesets", rulesets, "rest:enterprise/rulesets")

            sec_cfgs = self._safe_paginate(f"{self.base_url}/enterprises/{ent}/code-security/configurations", {"per_page": 100})
            if isinstance(sec_cfgs, list):
                summary = "; ".join(f"{c.get('name')}(enforcement={c.get('enforcement')})" for c in sec_cfgs if isinstance(c, dict))
                _add("security_configurations", "code_security_configs_count", len(sec_cfgs), "rest:code-security/configurations", details=summary)
            else:
                _add("security_configurations", "code_security_configs", sec_cfgs, "rest:code-security/configurations")

            # 4. Enterprise Custom Property Schemas & Teams
            for prop_ep, label in (
                (f"{self.base_url}/enterprises/{ent}/properties/schema", "repo_custom_property_schemas"),
                (f"{self.base_url}/enterprises/{ent}/org-properties/schema", "org_custom_property_schemas"),
            ):
                props = self._safe_paginate(prop_ep, {"per_page": 100})
                if isinstance(props, list):
                    names = ";".join(p.get("property_name", "") for p in props if isinstance(p, dict))
                    _add("custom_properties", label, len(props), f"rest:{label}", details=names)

            ent_teams = self._safe_paginate(f"{self.base_url}/enterprises/{ent}/teams", {"per_page": 100})
            if isinstance(ent_teams, list):
                _add("enterprise_iam", "enterprise_teams_count", len(ent_teams), "rest:enterprise/teams", details=";".join(t.get("slug", "") for t in ent_teams if isinstance(t, dict)))

            # 5. Copilot, Network Configs & Licensing
            copilot = self._safe_get_json(f"{self.base_url}/enterprises/{ent}/copilot/billing")
            if isinstance(copilot, dict) and copilot:
                for ck in ("seat_management_setting", "public_code_suggestions", "ide_chat", "platform_chat", "cli"):
                    if ck in copilot:
                        _add("copilot_policies", ck, copilot.get(ck), "rest:copilot/billing")

            net_cfgs = self._safe_get_json(f"{self.base_url}/enterprises/{ent}/network-configurations")
            if isinstance(net_cfgs, dict) and "network_configurations" in net_cfgs:
                cfgs = net_cfgs["network_configurations"]
                _add("network_configurations", "hosted_compute_network_configs", len(cfgs), "rest:network-configurations", details=";".join(c.get("name", "") for c in cfgs))

            lic = self._safe_get_json(f"{self.base_url}/enterprises/{ent}/consumed-licenses")
            if isinstance(lic, dict) and "total_seats_consumed" in lic:
                _add("licensing", "seats_consumed_vs_purchased", f"{lic.get('total_seats_consumed')}/{lic.get('total_seats_purchased')}", "rest:consumed-licenses")

        # 6. GHES Instance-Level Settings (Self-Hosted only)
        if self.deployment_type == "self_hosted":
            meta = self._safe_get_json(f"{self.base_url}/meta")
            if isinstance(meta, dict):
                _add("ghes_instance", "installed_version", meta.get("installed_version"), "rest:meta")
                _add("ghes_instance", "verifiable_password_authentication", meta.get("verifiable_password_authentication"), "rest:meta")
            ghes_lic = self._safe_get_json(f"{self.base_url}/enterprise/settings/license")
            if isinstance(ghes_lic, dict):
                _add("ghes_instance", "license_seats", f"{ghes_lic.get('seats_used')}/{ghes_lic.get('seats')}", "rest:enterprise/settings/license", details=f"expires={ghes_lic.get('expire_at')}")

        return rows

    # ---------- Extended Inventory & Migration Reports ----------

    def list_languages(self, repo: Dict[str, Any]) -> Dict[str, Any]:
        langs = self._safe_get_json(f"{self.base_url}/repos/{repo['full_name']}/languages")
        if not isinstance(langs, dict):
            return {"platform": self.platform, "org": self.org_or_project, "repo": repo["name"], "full_name": repo["full_name"], "primary_language": "", "total_bytes": 0, "languages_breakdown": str(langs)}
        total = sum(v for v in langs.values() if isinstance(v, int))
        ordered = sorted(langs.items(), key=lambda kv: kv[1], reverse=True)
        breakdown = ";".join(f"{k}:{round(v / total * 100, 1)}%" for k, v in ordered) if total else ""
        return {
            "platform": self.platform,
            "org": self.org_or_project,
            "repo": repo["name"],
            "full_name": repo["full_name"],
            "primary_language": ordered[0][0] if ordered else "",
            "total_bytes": total,
            "languages_breakdown": breakdown,
        }

    def list_releases_tags(self, repo: Dict[str, Any]) -> Dict[str, Any]:
        fn = repo["full_name"]
        rels = self._safe_paginate(f"{self.base_url}/repos/{fn}/releases", {"per_page": 100})
        tags = self._safe_paginate(f"{self.base_url}/repos/{fn}/tags", {"per_page": 100}, max_pages=5)
        rel_list = rels if isinstance(rels, list) else []
        tag_list = tags if isinstance(tags, list) else []
        assets_count = 0
        assets_bytes = 0
        for rel in rel_list:
            for a in rel.get("assets", []):
                assets_count += 1
                assets_bytes += int(a.get("size") or 0)
        return {
            "platform": self.platform,
            "org": self.org_or_project,
            "repo": repo["name"],
            "full_name": fn,
            "releases_count": len(rel_list),
            "latest_release_tag": rel_list[0].get("tag_name", "") if rel_list else "",
            "latest_release_date": rel_list[0].get("published_at", "") if rel_list else "",
            "tags_sampled_count": len(tag_list),
            "binary_assets_count": assets_count,
            "binary_assets_mb": round(assets_bytes / (1024 * 1024), 2),
        }

    def list_submodules(self, repo: Dict[str, Any]) -> List[Dict[str, Any]]:
        txt = self._read_file_text(repo["full_name"], ".gitmodules")
        if not txt:
            return []
        paths = [m.strip() for m in SUBMODULE_PATH_RE.findall(txt)]
        urls = [m.strip() for m in SUBMODULE_URL_RE.findall(txt)]
        rows = []
        for idx, url in enumerate(urls):
            rows.append({
                "platform": self.platform,
                "org": self.org_or_project,
                "repo": repo["name"],
                "full_name": repo["full_name"],
                "submodule_path": paths[idx] if idx < len(paths) else "",
                "submodule_url": url,
            })
        return rows

    # ---------- Extended IAM & Integrations ----------

    def list_teams_members(self) -> List[Dict[str, Any]]:
        teams = self._safe_paginate(f"{self.base_url}/orgs/{self.org_or_project}/teams", {"per_page": 100})
        if not isinstance(teams, list):
            return []
        rows = []
        for t in teams:
            slug = t.get("slug", "")
            members = self._safe_paginate(f"{self.base_url}/orgs/{self.org_or_project}/teams/{slug}/members", {"per_page": 100})
            member_logins = [m.get("login", "") for m in members if isinstance(m, dict)] if isinstance(members, list) else []
            rows.append({
                "platform": self.platform,
                "org": self.org_or_project,
                "team_slug": slug,
                "team_name": t.get("name", ""),
                "privacy": t.get("privacy", ""),
                "parent_team": (t.get("parent") or {}).get("slug", ""),
                "permission": t.get("permission", ""),
                "members_count": len(member_logins),
                "members": ";".join(member_logins),
            })
        return rows

    def list_webhooks_deploy_keys(self, repo: Dict[str, Any]) -> List[Dict[str, Any]]:
        fn = repo["full_name"]
        rows: List[Dict[str, Any]] = []
        hooks = self._safe_paginate(f"{self.base_url}/repos/{fn}/hooks", {"per_page": 100})
        if isinstance(hooks, list):
            for h in hooks:
                cfg = h.get("config") or {}
                rows.append({
                    "platform": self.platform,
                    "org": self.org_or_project,
                    "repo": repo["name"],
                    "full_name": fn,
                    "item_type": "webhook",
                    "item_id": str(h.get("id", "")),
                    "target_or_title": cfg.get("url", ""),
                    "active_or_readonly": f"active={h.get('active')};insecure_ssl={cfg.get('insecure_ssl')}",
                    "events_or_created": ";".join(h.get("events", [])),
                })
        keys = self._safe_paginate(f"{self.base_url}/repos/{fn}/keys", {"per_page": 100})
        if isinstance(keys, list):
            for k in keys:
                rows.append({
                    "platform": self.platform,
                    "org": self.org_or_project,
                    "repo": repo["name"],
                    "full_name": fn,
                    "item_type": "deploy_key",
                    "item_id": str(k.get("id", "")),
                    "target_or_title": k.get("title", ""),
                    "active_or_readonly": f"read_only={k.get('read_only')};verified={k.get('verified')}",
                    "events_or_created": k.get("created_at", ""),
                })
        return rows

    def list_iam_tokens_sso(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        pats = self._safe_paginate(f"{self.base_url}/orgs/{self.org_or_project}/personal-access-tokens", {"per_page": 100})
        if isinstance(pats, list):
            for p in pats:
                rows.append({
                    "platform": self.platform,
                    "org": self.org_or_project,
                    "record_type": "fine_grained_pat",
                    "id": str(p.get("id", "")),
                    "owner": (p.get("owner") or {}).get("login", ""),
                    "credential_type": p.get("repository_selection", ""),
                    "expires_or_authorized_at": p.get("token_expires_at", ""),
                    "last_used_at": p.get("token_last_used_at", ""),
                    "expired": p.get("token_expired", ""),
                })
        creds = self._safe_paginate(f"{self.base_url}/orgs/{self.org_or_project}/credential-authorizations", {"per_page": 100})
        if isinstance(creds, list):
            for c in creds:
                rows.append({
                    "platform": self.platform,
                    "org": self.org_or_project,
                    "record_type": "saml_credential_auth",
                    "id": str(c.get("credential_id", "")),
                    "owner": c.get("login", ""),
                    "credential_type": c.get("credential_type", ""),
                    "expires_or_authorized_at": c.get("credential_authorized_at", ""),
                    "last_used_at": c.get("credential_accessed_at", ""),
                    "expired": "",
                })
        return rows

    def list_dormant_users(self, inactive_days: int = 90) -> List[Dict[str, Any]]:
        members = self._safe_paginate(f"{self.base_url}/orgs/{self.org_or_project}/members", {"per_page": 100})
        if not isinstance(members, list):
            return []
        events = self._safe_paginate(f"{self.base_url}/orgs/{self.org_or_project}/events", {"per_page": 100}, max_pages=3)
        last_seen: Dict[str, str] = {}
        if isinstance(events, list):
            for ev in events:
                actor = (ev.get("actor") or {}).get("login", "")
                ts = ev.get("created_at", "")
                if actor and (actor not in last_seen or ts > last_seen[actor]):
                    last_seen[actor] = ts
        now = datetime.now(timezone.utc)
        rows = []
        for m in members:
            login = m.get("login", "")
            seen = last_seen.get(login, "")
            days_idle: Any = "no_recent_org_events"
            dormant = True
            if seen:
                dt = datetime.fromisoformat(seen.replace("Z", "+00:00"))
                days_idle = (now - dt).days
                dormant = days_idle >= inactive_days
            rows.append({
                "platform": self.platform,
                "org": self.org_or_project,
                "login": login,
                "user_type": m.get("type", ""),
                "last_seen_org_event": seen,
                "days_since_event": days_idle,
                "likely_dormant": dormant,
            })
        return rows

    # ---------- Security & Compliance ----------

    def list_security_alerts(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        sources = [
            ("code_scanning", f"{self.base_url}/orgs/{self.org_or_project}/code-scanning/alerts"),
            ("secret_scanning", f"{self.base_url}/orgs/{self.org_or_project}/secret-scanning/alerts"),
            ("dependabot", f"{self.base_url}/orgs/{self.org_or_project}/dependabot/alerts"),
        ]
        for alert_type, url in sources:
            alerts = self._safe_paginate(url, {"state": "open", "per_page": 100}, max_pages=10)
            if isinstance(alerts, list):
                for a in alerts:
                    repo_name = (a.get("repository") or {}).get("full_name", "")
                    severity = (
                        (a.get("rule") or {}).get("security_severity_level")
                        or (a.get("security_advisory") or {}).get("severity")
                        or ("high" if alert_type == "secret_scanning" else "")
                    )
                    summary = (
                        (a.get("rule") or {}).get("id")
                        or a.get("secret_type")
                        or (a.get("security_advisory") or {}).get("ghsa_id")
                        or ""
                    )
                    rows.append({
                        "platform": self.platform,
                        "org": self.org_or_project,
                        "alert_type": alert_type,
                        "alert_number": str(a.get("number", "")),
                        "repo": repo_name,
                        "state": a.get("state", ""),
                        "severity": severity,
                        "rule_or_secret_type": summary,
                        "created_at": a.get("created_at", ""),
                        "html_url": a.get("html_url", ""),
                    })
        return rows

    def list_org_secrets_variables(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for kind, key in (("secrets", "secrets"), ("variables", "variables")):
            pages = self._safe_paginate(f"{self.base_url}/orgs/{self.org_or_project}/actions/{kind}", {"per_page": 100})
            if isinstance(pages, list):
                for p in pages:
                    for item in (p.get(key, []) if isinstance(p, dict) else []):
                        rows.append({
                            "platform": self.platform,
                            "org": self.org_or_project,
                            "kind": f"org_actions_{kind[:-1]}",
                            "name": item.get("name", ""),
                            "visibility": item.get("visibility", ""),
                            "updated_at": item.get("updated_at", ""),
                            "value_preview": item.get("value", "") if kind == "variables" else "[REDACTED]",
                        })
        return rows

    def audit_sbom_licenses(self, repo: Dict[str, Any]) -> Dict[str, Any]:
        fn = repo["full_name"]
        raw = repo.get("_raw") or {}
        lic = (raw.get("license") or {}).get("spdx_id") or "NONE"
        sbom = self._safe_get_json(f"{self.base_url}/repos/{fn}/dependency-graph/sbom")
        packages = ((sbom or {}).get("sbom") or {}).get("packages", []) if isinstance(sbom, dict) else []
        dep_licenses = sorted({p.get("licenseConcluded", "") for p in packages if p.get("licenseConcluded") and p.get("licenseConcluded") != "NOASSERTION"})
        return {
            "platform": self.platform,
            "org": self.org_or_project,
            "repo": repo["name"],
            "full_name": fn,
            "repo_license": lic,
            "sbom_available": isinstance(sbom, dict) and "sbom" in sbom,
            "dependency_count": len(packages),
            "dependency_licenses": ";".join(dep_licenses[:30]),
        }

    def audit_actions_pinning(self, repo: Dict[str, Any]) -> List[Dict[str, Any]]:
        fn = repo["full_name"]
        wf_dir = self._safe_get_json(f"{self.base_url}/repos/{fn}/contents/.github/workflows")
        if not isinstance(wf_dir, list):
            return []
        rows: List[Dict[str, Any]] = []
        for f in wf_dir:
            name = f.get("name", "")
            if not name.lower().endswith((".yml", ".yaml")):
                continue
            content = self._read_file_text(fn, f".github/workflows/{name}")
            for ref in USES_RE.findall(content):
                if ref.startswith("./") or ref.startswith("docker://"):
                    continue
                action, _, version = ref.partition("@")
                pinned = bool(SHA_RE.match(version))
                rows.append({
                    "platform": self.platform,
                    "org": self.org_or_project,
                    "repo": repo["name"],
                    "full_name": fn,
                    "workflow_file": name,
                    "action": action,
                    "ref": version or "UNSPECIFIED",
                    "pinned_to_sha": pinned,
                })
        return rows

    def list_audit_log(self, since: Optional[str] = None, until: Optional[str] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"include": "all", "per_page": 100}
        if since and until:
            params["phrase"] = f"created:{since[:10]}..{until[:10]}"
        elif since:
            params["phrase"] = f"created:>={since[:10]}"
        entries = self._safe_paginate(f"{self.base_url}/orgs/{self.org_or_project}/audit-log", params=params, max_pages=10)
        if not isinstance(entries, list):
            return []
        return [
            {
                "platform": self.platform,
                "org": self.org_or_project,
                "document_id": e.get("_document_id", ""),
                "action": e.get("action", ""),
                "actor": e.get("actor", ""),
                "repo": e.get("repo", ""),
                "created_at": e.get("@timestamp", ""),
                "actor_ip": e.get("actor_ip", ""),
            }
            for e in entries
            if isinstance(e, dict)
        ]

    # ---------- CI/CD, Runners, Environments & Pages ----------

    def list_runners_packages(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        runners = self._safe_paginate(f"{self.base_url}/orgs/{self.org_or_project}/actions/runners", {"per_page": 100})
        if isinstance(runners, list):
            for p in runners:
                for r in (p.get("runners", []) if isinstance(p, dict) else []):
                    rows.append({
                        "platform": self.platform,
                        "org": self.org_or_project,
                        "resource_type": "self_hosted_runner",
                        "name": r.get("name", ""),
                        "status_or_visibility": f"{r.get('status')}|busy={r.get('busy')}",
                        "details": f"os={r.get('os')};labels={','.join(l.get('name', '') for l in r.get('labels', []))}",
                    })
        cache = self._safe_get_json(f"{self.base_url}/orgs/{self.org_or_project}/actions/cache/usage")
        if isinstance(cache, dict) and "total_active_caches_size_in_bytes" in cache:
            rows.append({
                "platform": self.platform,
                "org": self.org_or_project,
                "resource_type": "actions_cache_usage",
                "name": "org_total_cache",
                "status_or_visibility": f"count={cache.get('total_active_caches_count')}",
                "details": f"size_mb={round((cache.get('total_active_caches_size_in_bytes') or 0) / (1024 * 1024), 2)}",
            })
        for pkg_type in ("container", "npm", "maven", "nuget", "rubygems"):
            pkgs = self._safe_paginate(
                f"{self.base_url}/orgs/{self.org_or_project}/packages",
                {"package_type": pkg_type, "per_page": 100},
            )
            if isinstance(pkgs, list):
                for pkg in pkgs:
                    rows.append({
                        "platform": self.platform,
                        "org": self.org_or_project,
                        "resource_type": f"package:{pkg_type}",
                        "name": pkg.get("name", ""),
                        "status_or_visibility": pkg.get("visibility", ""),
                        "details": f"repo={(pkg.get('repository') or {}).get('name', '')};updated={pkg.get('updated_at', '')}",
                    })
        return rows

    def list_environments(self, repo: Dict[str, Any]) -> List[Dict[str, Any]]:
        fn = repo["full_name"]
        env_pages = self._safe_paginate(f"{self.base_url}/repos/{fn}/environments", {"per_page": 100})
        if not isinstance(env_pages, list):
            return []
        rows: List[Dict[str, Any]] = []
        for p in env_pages:
            for env in (p.get("environments", []) if isinstance(p, dict) else []):
                env_name = env.get("name", "")
                rules = [r.get("type", "") for r in env.get("protection_rules", [])]
                sec = self._safe_get_json(f"{self.base_url}/repos/{fn}/environments/{quote(env_name, safe='')}/secrets")
                sec_names = [s.get("name", "") for s in sec.get("secrets", [])] if isinstance(sec, dict) else []
                rows.append({
                    "platform": self.platform,
                    "org": self.org_or_project,
                    "repo": repo["name"],
                    "full_name": fn,
                    "environment": env_name,
                    "protection_rules": ";".join(rules),
                    "env_secrets_count": len(sec_names),
                    "env_secret_names": ";".join(sec_names),
                })
        return rows

    def audit_workflow_runs(self, repo: Dict[str, Any]) -> Dict[str, Any]:
        fn = repo["full_name"]
        data = self._safe_get_json(f"{self.base_url}/repos/{fn}/actions/runs", {"per_page": 50})
        runs = data.get("workflow_runs", []) if isinstance(data, dict) else []
        success = sum(1 for r in runs if r.get("conclusion") == "success")
        failure = sum(1 for r in runs if r.get("conclusion") == "failure")
        durations_sec: List[float] = []
        for r in runs:
            hrs = hours_between(r.get("run_started_at") or r.get("created_at"), r.get("updated_at"))
            if hrs is not None and hrs >= 0:
                durations_sec.append(round(hrs * 3600, 1))
        total_completed = success + failure
        return {
            "platform": self.platform,
            "org": self.org_or_project,
            "repo": repo["name"],
            "full_name": fn,
            "sampled_runs": len(runs),
            "success_count": success,
            "failure_count": failure,
            "pass_rate_pct": round(success / total_completed * 100, 1) if total_completed else 0.0,
            "avg_duration_sec": round(sum(durations_sec) / len(durations_sec), 1) if durations_sec else 0.0,
            "p95_duration_sec": percentile(durations_sec, 95),
        }

    def audit_pages(self, repo: Dict[str, Any]) -> Dict[str, Any]:
        fn = repo["full_name"]
        data = self._safe_get_json(f"{self.base_url}/repos/{fn}/pages")
        if not isinstance(data, dict) or not data.get("html_url"):
            return {"platform": self.platform, "org": self.org_or_project, "repo": repo["name"], "full_name": fn, "pages_enabled": False, "pages_url": "", "custom_domain": "", "https_enforced": False}
        return {
            "platform": self.platform,
            "org": self.org_or_project,
            "repo": repo["name"],
            "full_name": fn,
            "pages_enabled": True,
            "pages_url": data.get("html_url", ""),
            "custom_domain": data.get("cname") or "",
            "https_enforced": bool(data.get("https_enforced")),
        }

    # ---------- Engineering Metrics & DORA ----------

    def list_pull_requests(
        self, repo: Dict[str, Any], since: Optional[str] = None, until: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        fn = repo["full_name"]
        rows: List[Dict[str, Any]] = []
        next_url: Optional[str] = f"{self.base_url}/repos/{fn}/pulls"
        params: Dict[str, Any] = {"state": "all", "sort": "updated", "direction": "desc", "per_page": 100}

        while next_url:
            resp = self.http.request("GET", next_url, params=params, allow_404=True, allow_403=True)
            if not resp or resp.status_code != 200:
                break
            prs = resp.json()
            if not prs:
                break
            stop_early = False
            for pr in prs:
                updated = pr.get("updated_at") or ""
                created = pr.get("created_at") or ""
                if since and updated and updated < since:
                    stop_early = True
                    break
                if since and created < since:
                    continue
                if until and created > until:
                    continue
                merged = pr.get("merged_at") or ""
                rows.append({
                    "platform": self.platform,
                    "org": self.org_or_project,
                    "repo": repo["name"],
                    "pr_id": str(pr.get("number")),
                    "state": pr.get("state", ""),
                    "draft": bool(pr.get("draft")),
                    "author": (pr.get("user") or {}).get("login", ""),
                    "created_at": created,
                    "updated_at": updated,
                    "merged_at": merged,
                    "closed_at": pr.get("closed_at") or "",
                    "cycle_time_hours": hours_between(created, merged),
                })
            if stop_early:
                break
            next_url = resp.links.get("next", {}).get("url")
            params = {}
        return rows

    def audit_pr_reviews(
        self, repo: Dict[str, Any], since: Optional[str] = None, until: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        prs = self.list_pull_requests(repo, since=since, until=until)[:30]
        rows: List[Dict[str, Any]] = []
        for pr in prs:
            revs = self._safe_paginate(f"{self.base_url}/repos/{repo['full_name']}/pulls/{pr['pr_id']}/reviews", {"per_page": 100})
            rev_list = revs if isinstance(revs, list) else []
            submitted = sorted([r.get("submitted_at") for r in rev_list if r.get("submitted_at")])
            approvals = sum(1 for r in rev_list if r.get("state") == "APPROVED")
            reviewers = sorted({(r.get("user") or {}).get("login", "") for r in rev_list if (r.get("user") or {}).get("login")})
            rows.append({
                "platform": self.platform,
                "org": self.org_or_project,
                "repo": repo["name"],
                "pr_id": pr["pr_id"],
                "author": pr["author"],
                "created_at": pr["created_at"],
                "first_review_at": submitted[0] if submitted else "",
                "hours_to_first_review": hours_between(pr["created_at"], submitted[0] if submitted else None),
                "review_count": len(rev_list),
                "approvals_count": approvals,
                "reviewers": ";".join(reviewers),
            })
        return rows

    def audit_contributors_bus_factor(self, repo: Dict[str, Any]) -> Dict[str, Any]:
        fn = repo["full_name"]
        contribs = self._safe_paginate(f"{self.base_url}/repos/{fn}/contributors", {"per_page": 100}, max_pages=2)
        if not isinstance(contribs, list) or not contribs:
            return {"platform": self.platform, "org": self.org_or_project, "repo": repo["name"], "full_name": fn, "contributors_count": 0, "top_contributor": "", "top_contributor_share_pct": 0.0, "bus_factor_risk": "unknown"}
        total_commits = sum(int(c.get("contributions") or 0) for c in contribs if isinstance(c, dict))
        top = contribs[0]
        top_commits = int(top.get("contributions") or 0)
        share = round(top_commits / total_commits * 100, 1) if total_commits else 0.0
        risk = "HIGH" if share >= 80.0 or len(contribs) <= 1 else ("MEDIUM" if share >= 50.0 else "LOW")
        return {
            "platform": self.platform,
            "org": self.org_or_project,
            "repo": repo["name"],
            "full_name": fn,
            "contributors_count": len(contribs),
            "total_commits_counted": total_commits,
            "top_contributor": top.get("login", ""),
            "top_contributor_share_pct": share,
            "bus_factor_risk": risk,
        }

    def audit_issues_sla(
        self, repo: Dict[str, Any], since: Optional[str] = None, until: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"state": "all", "per_page": 100}
        if since:
            params["since"] = since
        items = self._safe_paginate(f"{self.base_url}/repos/{repo['full_name']}/issues", params=params, max_pages=3)
        if not isinstance(items, list):
            return []
        rows: List[Dict[str, Any]] = []
        for iss in items:
            if "pull_request" in iss:
                continue
            created = iss.get("created_at", "")
            if until and created > until:
                continue
            closed = iss.get("closed_at") or ""
            rows.append({
                "platform": self.platform,
                "org": self.org_or_project,
                "repo": repo["name"],
                "issue_number": str(iss.get("number", "")),
                "state": iss.get("state", ""),
                "author": (iss.get("user") or {}).get("login", ""),
                "labels": ";".join(l.get("name", "") for l in iss.get("labels", [])),
                "created_at": created,
                "closed_at": closed,
                "time_to_close_hours": hours_between(created, closed),
            })
        return rows

    def audit_dora_metrics(
        self, repo: Dict[str, Any], since: Optional[str] = None, until: Optional[str] = None
    ) -> Dict[str, Any]:
        fn = repo["full_name"]
        prs = self.list_pull_requests(repo, since=since, until=until)
        lead_times = [p["cycle_time_hours"] for p in prs if p.get("cycle_time_hours") is not None]
        deps = self._safe_paginate(f"{self.base_url}/repos/{fn}/deployments", {"per_page": 100}, max_pages=3)
        dep_list = deps if isinstance(deps, list) else []
        if since:
            dep_list = [d for d in dep_list if (d.get("created_at") or "") >= since]
        if until:
            dep_list = [d for d in dep_list if (d.get("created_at") or "") <= until]
        rels = self._safe_paginate(f"{self.base_url}/repos/{fn}/releases", {"per_page": 100}, max_pages=2)
        rel_list = rels if isinstance(rels, list) else []
        if since:
            rel_list = [r for r in rel_list if (r.get("published_at") or "") >= since]
        return {
            "platform": self.platform,
            "org": self.org_or_project,
            "repo": repo["name"],
            "full_name": fn,
            "deployments_count": len(dep_list),
            "releases_count": len(rel_list),
            "merged_prs_count": len(lead_times),
            "avg_pr_lead_time_hours": round(sum(lead_times) / len(lead_times), 2) if lead_times else 0.0,
            "p95_pr_lead_time_hours": percentile(lead_times, 95),
        }

    # ---------- Billing, Licensing & Copilot ----------

    def list_billing_copilot(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        endpoints = [
            ("billing_actions", f"{self.base_url}/orgs/{self.org_or_project}/settings/billing/actions"),
            ("billing_packages", f"{self.base_url}/orgs/{self.org_or_project}/settings/billing/packages"),
            ("billing_storage", f"{self.base_url}/orgs/{self.org_or_project}/settings/billing/shared-storage"),
            ("ghas_committers", f"{self.base_url}/orgs/{self.org_or_project}/settings/billing/advanced-security"),
            ("copilot_billing", f"{self.base_url}/orgs/{self.org_or_project}/copilot/billing"),
        ]
        for category, url in endpoints:
            data = self._safe_get_json(url)
            if isinstance(data, dict) and data:
                rows.append({"platform": self.platform, "org": self.org_or_project, "category": category, **data})

        seats = self._safe_paginate(f"{self.base_url}/orgs/{self.org_or_project}/copilot/billing/seats", {"per_page": 100})
        if isinstance(seats, list):
            for page in seats:
                for s in (page.get("seats", []) if isinstance(page, dict) else []):
                    rows.append({
                        "platform": self.platform,
                        "org": self.org_or_project,
                        "category": "copilot_seat",
                        "assignee": (s.get("assignee") or {}).get("login", ""),
                        "created_at": s.get("created_at", ""),
                        "last_activity_at": s.get("last_activity_at", ""),
                        "last_activity_editor": s.get("last_activity_editor", ""),
                    })

        codespaces = self._safe_paginate(f"{self.base_url}/orgs/{self.org_or_project}/codespaces", {"per_page": 100})
        if isinstance(codespaces, list):
            for page in codespaces:
                for cs in (page.get("codespaces", []) if isinstance(page, dict) else []):
                    rows.append({
                        "platform": self.platform,
                        "org": self.org_or_project,
                        "category": "codespace",
                        "assignee": (cs.get("owner") or {}).get("login", ""),
                        "repo": (cs.get("repository") or {}).get("full_name", ""),
                        "state": cs.get("state", ""),
                        "machine": (cs.get("machine") or {}).get("name", ""),
                        "last_used_at": cs.get("last_used_at", ""),
                    })
        return rows

    # ---------- Universal Escape Hatches (Raw REST + GraphQL) ----------

    def raw_query(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        repo: Optional[Dict[str, Any]] = None,
        extract_key: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        fmt = {
            "org": self.org_or_project,
            "enterprise": self.enterprise or self.org_or_project,
            "repo": repo["name"] if repo else "",
            "full_name": repo["full_name"] if repo else "",
        }
        ep = endpoint.format(**fmt).lstrip("/")
        qparams = dict(params or {})
        qparams.setdefault("per_page", 100)
        data = self._safe_paginate(f"{self.base_url}/{ep}", params=qparams)
        if not isinstance(data, list):
            return [{"platform": self.platform, "org": self.org_or_project, "repo": fmt["repo"], "error": str(data)}]

        rows: List[Dict[str, Any]] = []
        base_ctx = {"platform": self.platform, "org": self.org_or_project}
        if repo:
            base_ctx["repo"] = repo["name"]
            base_ctx["full_name"] = repo["full_name"]

        for item in data:
            if isinstance(item, dict):
                if extract_key and isinstance(item.get(extract_key), list):
                    for sub in item[extract_key]:
                        rows.append({**base_ctx, **(sub if isinstance(sub, dict) else {"value": sub})})
                    continue
                list_keys = [k for k, v in item.items() if isinstance(v, list)]
                if not extract_key and len(list_keys) == 1 and len(item) <= 4:
                    for sub in item[list_keys[0]]:
                        rows.append({**base_ctx, **(sub if isinstance(sub, dict) else {"value": sub})})
                    continue
                rows.append({**base_ctx, **item})
            else:
                rows.append({**base_ctx, "value": item})
        return rows

    def graphql_query(self, query: str, variables: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        vars_payload = dict(variables or {})
        vars_payload.setdefault("org", self.org_or_project)
        if self.enterprise:
            vars_payload.setdefault("slug", self.enterprise)
        resp = self.http.request(
            "POST",
            self.graphql_url,
            json_body={"query": query, "variables": vars_payload},
        )
        if not resp:
            return []
        payload = resp.json()
        if payload.get("errors"):
            raise RuntimeError(f"GraphQL errors: {payload['errors']}")
        data = payload.get("data", {})
        return [{"platform": self.platform, "org": self.org_or_project, **(data if isinstance(data, dict) else {"data": data})}]