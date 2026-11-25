from datetime import datetime, timezone
import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple, Union
from datascrape.clients.base import VCSClient, hours_between, percentile
from datascrape.http_client import ResilientHTTPClient

SUBMODULE_URL_RE = re.compile(r"^\s*url\s*=\s*(.+)$", re.MULTILINE)
SUBMODULE_PATH_RE = re.compile(r"^\s*path\s*=\s*(.+)$", re.MULTILINE)


class BitbucketClient(VCSClient):
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
        backoff_base: float = 2.0,
        backoff_max: float = 900.0,
    ):
        clean_url = (base_url or "https://api.bitbucket.org/2.0").rstrip("/")
        if deployment_type == "cloud":
            api_url = "https://api.bitbucket.org/2.0"
        else:
            api_url = clean_url if clean_url.endswith("/rest/api/1.0") else f"{clean_url}/rest/api/1.0"

        super().__init__(
            "bitbucket",
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
        headers = {"Accept": "application/json"}
        auth: Optional[Tuple[str, str]] = None
        if auth_type == "basic":
            if ":" not in token:
                raise ValueError("Bitbucket basic authentication requires a token formatted as username:app_password.")
            user, pwd = token.split(":", 1)
            auth = (user, pwd)
        else:
            headers["Authorization"] = f"Bearer {token}"

        identity = hashlib.sha256(f"{api_url}:{token}".encode()).hexdigest()
        self.http = ResilientHTTPClient(
            headers=headers,
            auth=auth,
            max_retries=max_retries,
            base_delay=backoff_base,
            max_delay=backoff_max,
            timeout=timeout,
            verify_ssl=verify_ssl,
            min_rate_buffer=rate_buffer,
            coordinator_key=f"bitbucket:{api_url}:{identity}",
        )

    def _paginate_cloud(self, url: str, max_pages: Optional[int] = None) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        next_url: Optional[str] = url
        pages = 0
        while next_url:
            resp = self.http.request("GET", next_url, allow_404=True, allow_403=True)
            if not resp or resp.status_code != 200:
                break
            data = resp.json()
            items.extend(data.get("values", []))
            pages += 1
            if max_pages and pages >= max_pages:
                break
            next_url = data.get("next")
        return items

    def _paginate_server(
        self, url: str, params: Optional[Dict[str, Any]] = None, max_pages: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        start = 0
        pages = 0
        while True:
            p = dict(params or {}, start=start, limit=100)
            resp = self.http.request("GET", url, params=p, allow_404=True, allow_403=True)
            if not resp or resp.status_code != 200:
                break
            data = resp.json()
            items.extend(data.get("values", []))
            pages += 1
            if data.get("isLastPage", True) or (max_pages and pages >= max_pages):
                break
            start = data.get("nextPageStart", start + 100)
        return items

    def _file_exists(self, repo: Dict[str, Any], path: str) -> bool:
        url = (
            f"{self.base_url}/repositories/{repo['full_name']}/src/HEAD/{path}"
            if self.deployment_type == "cloud"
            else f"{self.base_url}/projects/{self.org_or_project}/repos/{repo['name']}/browse/{path}"
        )
        resp = self.http.request("GET", url, allow_404=True, allow_403=True)
        return resp is not None and resp.status_code == 200

    def _file_text(self, repo: Dict[str, Any], path: str) -> str:
        url = (
            f"{self.base_url}/repositories/{repo['full_name']}/src/HEAD/{path}"
            if self.deployment_type == "cloud"
            else f"{self.base_url}/projects/{self.org_or_project}/repos/{repo['name']}/raw/{path}"
        )
        resp = self.http.request("GET", url, allow_404=True, allow_403=True)
        return resp.text if (resp is not None and resp.status_code == 200) else ""

    def list_orgs(self) -> List[str]:
        if self.deployment_type == "cloud":
            return sorted({w["slug"] for w in self._paginate_cloud(f"{self.base_url}/workspaces") if w.get("slug")})
        return sorted({p["key"] for p in self._paginate_server(f"{self.base_url}/projects") if p.get("key")})

    def list_repos(self) -> List[Dict[str, Any]]:
        if self.deployment_type == "cloud":
            raw = self._paginate_cloud(f"{self.base_url}/repositories/{self.org_or_project}")
            return [
                {
                    "platform": self.platform,
                    "deployment_type": self.deployment_type,
                    "org": self.org_or_project,
                    "name": r["slug"],
                    "full_name": r["full_name"],
                    "visibility": "private" if r.get("is_private") else "public",
                    "primary_language": r.get("language") or "",
                    "default_branch": (r.get("mainbranch") or {}).get("name", "main"),
                    "archived": False,
                    "pushed_at": r.get("updated_on"),
                    "size_kb": int((r.get("size") or 0) / 1024),
                    "html_url": r.get("links", {}).get("html", {}).get("href", ""),
                    "_raw": r,
                }
                for r in raw
            ]

        raw = self._paginate_server(f"{self.base_url}/projects/{self.org_or_project}/repos")
        return [
            {
                "platform": self.platform,
                "deployment_type": self.deployment_type,
                "org": self.org_or_project,
                "name": r["slug"],
                "full_name": f"{self.org_or_project}/{r['slug']}",
                "visibility": "public" if r.get("public") else "private",
                "primary_language": "",
                "default_branch": "main",
                "archived": bool(r.get("archived", False)),
                "pushed_at": None,
                "size_kb": 0,
                "html_url": (r.get("links", {}).get("self") or [{}])[0].get("href", ""),
                "_raw": r,
            }
            for r in raw
        ]

    def check_owner_and_ci(self, repo: Dict[str, Any]) -> Dict[str, Any]:
        has_co = self._file_exists(repo, "CODEOWNERS")
        ci_types = []
        for path, label in [("bitbucket-pipelines.yml", "bitbucket_pipelines"), ("Jenkinsfile", "jenkins")] + [
            (p, f"custom:{p}") for p in self.custom_ci_files
        ]:
            if self._file_exists(repo, path):
                ci_types.append(label)
        return {
            "has_codeowners": has_co,
            "custom_property_owner": "",
            "has_owner_signal": has_co,
            "has_ci": bool(ci_types),
            "ci_type": ";".join(ci_types) if ci_types else "none",
        }

    def list_apps_and_service_accounts(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if self.deployment_type == "cloud":
            for m in self._paginate_cloud(f"{self.base_url}/workspaces/{self.org_or_project}/members"):
                u = m.get("user", {})
                rows.append({
                    "platform": self.platform,
                    "org": self.org_or_project,
                    "record_type": "workspace_member",
                    "identifier": u.get("nickname") or u.get("display_name", ""),
                    "app_id": u.get("uuid", ""),
                    "scope": u.get("type", ""),
                    "suspended": "",
                    "updated_at": "",
                    "owner": "",
                })
        else:
            for u in self._paginate_server(f"{self.base_url}/projects/{self.org_or_project}/permissions/users"):
                user = u.get("user", {})
                rows.append({
                    "platform": self.platform,
                    "org": self.org_or_project,
                    "record_type": "project_user",
                    "identifier": user.get("name", ""),
                    "app_id": user.get("id", ""),
                    "scope": u.get("permission", ""),
                    "suspended": not user.get("active", True),
                    "updated_at": "",
                    "owner": "",
                })
        return rows

    def list_pull_requests(
        self, repo: Dict[str, Any], since: Optional[str] = None, until: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if self.deployment_type == "cloud":
            prs = self._paginate_cloud(f"{self.base_url}/repositories/{repo['full_name']}/pullrequests?state=ALL")
            for pr in prs:
                created = pr.get("created_on", "")
                if (since and created < since) or (until and created > until):
                    continue
                merged = pr.get("updated_on", "") if pr.get("state") == "MERGED" else ""
                rows.append({
                    "platform": self.platform,
                    "org": self.org_or_project,
                    "repo": repo["name"],
                    "pr_id": str(pr.get("id")),
                    "state": pr.get("state", ""),
                    "draft": False,
                    "author": (pr.get("author") or {}).get("display_name", ""),
                    "created_at": created,
                    "updated_at": pr.get("updated_on", ""),
                    "merged_at": merged,
                    "closed_at": pr.get("updated_on", "") if pr.get("state") in ("MERGED", "DECLINED") else "",
                    "cycle_time_hours": hours_between(created, merged),
                })
        else:
            prs = self._paginate_server(
                f"{self.base_url}/projects/{self.org_or_project}/repos/{repo['name']}/pull-requests", {"state": "ALL"}
            )
            for pr in prs:
                ms = pr.get("createdDate")
                created = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat() if ms else ""
                if (since and created < since) or (until and created > until):
                    continue
                closed_ms = pr.get("closedDate")
                closed = datetime.fromtimestamp(closed_ms / 1000, tz=timezone.utc).isoformat() if closed_ms else ""
                merged = closed if pr.get("state") == "MERGED" else ""
                rows.append({
                    "platform": self.platform,
                    "org": self.org_or_project,
                    "repo": repo["name"],
                    "pr_id": str(pr.get("id")),
                    "state": pr.get("state", ""),
                    "draft": False,
                    "author": ((pr.get("author") or {}).get("user") or {}).get("name", ""),
                    "created_at": created,
                    "updated_at": "",
                    "merged_at": merged,
                    "closed_at": closed,
                    "cycle_time_hours": hours_between(created, merged),
                })
        return rows

    def audit_repo(self, repo: Dict[str, Any]) -> Dict[str, Any]:
        lfs_enabled = "filter=lfs" in self._file_text(repo, ".gitattributes")
        ci_info = self.check_owner_and_ci(repo)
        prot_rules, teams_list, direct_list, admin_users, admin_teams = [], [], [], [], []

        if self.deployment_type == "cloud":
            fn = repo["full_name"]
            prot_rules = [
                f"{r.get('pattern')}:{r.get('kind')}"
                for r in self._paginate_cloud(f"{self.base_url}/repositories/{fn}/branch-restrictions")
            ]
            for p in self._paginate_cloud(f"{self.base_url}/workspaces/{self.org_or_project}/permissions/repositories/{repo['name']}"):
                u = (p.get("user") or {}).get("nickname", "")
                role = p.get("permission", "")
                direct_list.append(f"{u}:{role}")
                if role == "admin":
                    admin_users.append(u)
        else:
            base = f"{self.base_url}/projects/{self.org_or_project}/repos/{repo['name']}"
            for u in self._paginate_server(f"{base}/permissions/users"):
                name, perm = (u.get("user") or {}).get("name", ""), u.get("permission", "")
                direct_list.append(f"{name}:{perm}")
                if perm == "REPO_ADMIN":
                    admin_users.append(name)
            for g in self._paginate_server(f"{base}/permissions/groups"):
                gname, perm = (g.get("group") or {}).get("name", ""), g.get("permission", "")
                teams_list.append(f"{gname}:{perm}")
                if perm == "REPO_ADMIN":
                    admin_teams.append(gname)

        return {
            "platform": self.platform,
            "org": self.org_or_project,
            "repo": repo["name"],
            "full_name": repo["full_name"],
            "visibility": repo.get("visibility", ""),
            "archived": repo["archived"],
            "size_kb": repo["size_kb"],
            "size_mb": round(repo["size_kb"] / 1024, 2),
            "lfs_enabled": lfs_enabled,
            "actions_enabled": ci_info["has_ci"],
            "actions_allowed_policy": ci_info["ci_type"],
            "actions_default_token_perm": "",
            "actions_workflow_count": 1 if ci_info["has_ci"] else 0,
            "repo_secrets_count": "N/A",
            "repo_secret_names": "",
            "protected_branches": ";".join(prot_rules),
            "rulesets": "",
            "has_branch_protection_or_ruleset": bool(prot_rules),
            "teams_count": len(teams_list),
            "teams": ";".join(teams_list),
            "direct_assignments_count": len(direct_list),
            "direct_assignments": ";".join(direct_list),
            "outside_collaborators": "",
            "admin_users": ";".join(admin_users),
            "admin_teams": ";".join(admin_teams),
            "html_url": repo["html_url"],
        }

    def audit_repo_configurations(self, repo: Dict[str, Any]) -> Dict[str, Any]:
        raw = repo.get("_raw") or {}
        checks = {
            "has_readme": self._file_exists(repo, "README.md"),
            "has_codeowners": self._file_exists(repo, "CODEOWNERS"),
            "has_security_md": self._file_exists(repo, "SECURITY.md"),
            "has_workflows_dir": self._file_exists(repo, "bitbucket-pipelines.yml") or self._file_exists(repo, "Jenkinsfile"),
        }
        hooks = (
            self._paginate_cloud(f"{self.base_url}/repositories/{repo['full_name']}/hooks")
            if self.deployment_type == "cloud"
            else self._paginate_server(f"{self.base_url}/projects/{self.org_or_project}/repos/{repo['name']}/webhooks")
        )
        return {
            "platform": self.platform,
            "org": self.org_or_project,
            "repo": repo["name"],
            "full_name": repo["full_name"],
            "visibility": repo.get("visibility", ""),
            "default_branch": repo.get("default_branch", "main"),
            "archived": repo["archived"],
            "has_issues": bool(raw.get("has_issues")),
            "has_wiki": bool(raw.get("has_wiki")),
            "webhooks_count": len(hooks),
            **checks,
            "configs_present": ";".join(k.replace("has_", "") for k, v in checks.items() if v),
            "configs_missing": ";".join(k.replace("has_", "") for k, v in checks.items() if not v),
        }

    def audit_enterprise_policies(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if self.deployment_type == "self_hosted":
            resp = self.http.request("GET", f"{self.base_url}/application-properties", allow_403=True, allow_404=True)
            if resp and resp.status_code == 200:
                for k, v in resp.json().items():
                    rows.append({
                        "platform": self.platform,
                        "deployment_type": self.deployment_type,
                        "enterprise": self.enterprise or self.base_url,
                        "category": "server_properties",
                        "policy_name": k,
                        "current_value": v,
                        "status": "CONFIGURED",
                        "source_api": "rest:application-properties",
                        "details": "",
                    })
        else:
            rows.append({
                "platform": self.platform,
                "deployment_type": self.deployment_type,
                "enterprise": self.enterprise or self.org_or_project,
                "category": "workspace",
                "policy_name": "enterprise_layer_note",
                "current_value": "N/A",
                "status": "NOT_SUPPORTED",
                "source_api": "bitbucket_cloud",
                "details": "Bitbucket Cloud policies are managed at the Atlassian Admin / Workspace layer",
            })
        return rows

    def list_languages(self, repo: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "org": self.org_or_project,
            "repo": repo["name"],
            "full_name": repo["full_name"],
            "primary_language": repo.get("primary_language", ""),
            "total_bytes": "",
            "languages_breakdown": repo.get("primary_language", ""),
        }

    def list_submodules(self, repo: Dict[str, Any]) -> List[Dict[str, Any]]:
        txt = self._file_text(repo, ".gitmodules")
        if not txt:
            return []
        paths = [m.strip() for m in SUBMODULE_PATH_RE.findall(txt)]
        urls = [m.strip() for m in SUBMODULE_URL_RE.findall(txt)]
        return [
            {
                "platform": self.platform,
                "org": self.org_or_project,
                "repo": repo["name"],
                "full_name": repo["full_name"],
                "submodule_path": paths[i] if i < len(paths) else "",
                "submodule_url": url,
            }
            for i, url in enumerate(urls)
        ]

    def list_webhooks_deploy_keys(self, repo: Dict[str, Any]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        hooks = (
            self._paginate_cloud(f"{self.base_url}/repositories/{repo['full_name']}/hooks")
            if self.deployment_type == "cloud"
            else self._paginate_server(f"{self.base_url}/projects/{self.org_or_project}/repos/{repo['name']}/webhooks")
        )
        for h in hooks:
            rows.append({
                "platform": self.platform,
                "org": self.org_or_project,
                "repo": repo["name"],
                "full_name": repo["full_name"],
                "item_type": "webhook",
                "item_id": str(h.get("uuid") or h.get("id", "")),
                "target_or_title": h.get("url", ""),
                "active_or_readonly": f"active={h.get('active')}",
                "events_or_created": ";".join(h.get("events", [])),
            })
        return rows

    def audit_dora_metrics(
        self, repo: Dict[str, Any], since: Optional[str] = None, until: Optional[str] = None
    ) -> Dict[str, Any]:
        prs = self.list_pull_requests(repo, since=since, until=until)
        lead_times = [p["cycle_time_hours"] for p in prs if p.get("cycle_time_hours") is not None]
        return {
            "platform": self.platform,
            "org": self.org_or_project,
            "repo": repo["name"],
            "full_name": repo["full_name"],
            "deployments_count": 0,
            "releases_count": 0,
            "merged_prs_count": len(lead_times),
            "avg_pr_lead_time_hours": round(sum(lead_times) / len(lead_times), 2) if lead_times else 0.0,
            "p95_pr_lead_time_hours": percentile(lead_times, 95),
        }

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
        url = f"{self.base_url}/{ep}"
        items = self._paginate_cloud(url) if self.deployment_type == "cloud" else self._paginate_server(url, params)
        base_ctx = {"platform": self.platform, "org": self.org_or_project}
        if repo:
            base_ctx["repo"] = repo["name"]
            base_ctx["full_name"] = repo["full_name"]
        return [{**base_ctx, **i} for i in items]