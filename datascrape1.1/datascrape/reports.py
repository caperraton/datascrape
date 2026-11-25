from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from datascrape.clients.base import VCSClient
from datascrape.progress import ProgressTracker
from datascrape.storage import DBManager

REPORT_CATALOG: Dict[str, Dict[str, Any]] = {
    # Core Assessment
    "ownership_baseline": {"scope": "repo", "keys": ["platform", "org", "full_name"], "desc": "Active repos with CODEOWNERS, custom property owners & multi-CI detection"},
    "apps_service_accounts": {"scope": "org", "keys": ["platform", "org", "record_type", "identifier"], "desc": "GitHub Apps (with registered owners, permissions & audit-log installers) & service accounts"},
    "archived_inactive": {"scope": "repo", "keys": ["platform", "org", "full_name"], "desc": "Archived & inactive repos with size and proposed disposition"},
    "repo_audit": {"scope": "repo", "keys": ["platform", "org", "full_name"], "desc": "Size, LFS, Actions, secrets, branch protections/rulesets, teams, direct users, admins"},
    "repo_configurations": {"scope": "repo", "keys": ["platform", "org", "full_name"], "desc": "Present vs. missing repo settings, GHAS, security features & governance files"},
    "enterprise_policies": {"scope": "enterprise", "keys": ["platform", "enterprise", "category", "policy_name"], "desc": "Enterprise-layer configurations, governance policies, SSO/IP allow lists, Actions & rulesets"},
    # Extended Inventory
    "languages": {"scope": "repo", "keys": ["platform", "org", "full_name"], "desc": "Per-repo programming language breakdown & byte counts"},
    "releases_tags": {"scope": "repo", "keys": ["platform", "org", "full_name"], "desc": "Release counts, latest tags & attached binary asset sizes"},
    "submodules": {"scope": "repo", "keys": ["platform", "org", "full_name", "submodule_path"], "desc": "Git submodules parsed from .gitmodules"},
    # IAM & Integrations
    "teams_members": {"scope": "org", "keys": ["platform", "org", "team_slug"], "desc": "Org teams, parent hierarchy, privacy & member lists"},
    "webhooks_deploy_keys": {"scope": "repo", "keys": ["platform", "org", "full_name", "item_type", "item_id"], "desc": "Full webhook target URLs, SSL flags & deploy keys"},
    "iam_tokens_sso": {"scope": "org", "keys": ["platform", "org", "record_type", "id"], "desc": "Fine-grained PATs & SAML SSO credential authorizations"},
    "dormant_users": {"scope": "org", "keys": ["platform", "org", "login"], "desc": "Org members cross-referenced against recent org events"},
    # Security & Compliance
    "security_alerts": {"scope": "org", "keys": ["platform", "org", "alert_type", "repo", "alert_number"], "desc": "Open Code Scanning, Secret Scanning & Dependabot CVE alerts"},
    "org_secrets_variables": {"scope": "org", "keys": ["platform", "org", "kind", "name"], "desc": "Org-level Actions secrets & variables with visibility"},
    "sbom_licenses": {"scope": "repo", "keys": ["platform", "org", "full_name"], "desc": "SPDX repo licenses & SBOM dependency counts"},
    "actions_pinning": {"scope": "repo", "keys": ["platform", "org", "full_name", "workflow_file", "action", "ref"], "desc": "Supply-chain audit of GitHub Actions not pinned to 40-char commit SHAs"},
    "audit_log": {"scope": "org", "keys": ["platform", "org", "document_id"], "desc": "Org audit log export (supports --backfill-since/--backfill-until)"},
    # CI/CD, Runners & Packages
    "runners_packages": {"scope": "org", "keys": ["platform", "org", "resource_type", "name"], "desc": "Self-hosted runners, Actions cache footprint & Packages/GHCR"},
    "environments": {"scope": "repo", "keys": ["platform", "org", "full_name", "environment"], "desc": "Deployment environments, protection rules & env secrets"},
    "workflow_runs": {"scope": "repo", "keys": ["platform", "org", "full_name"], "desc": "Workflow pass/fail rates, average & p95 run durations"},
    "pages": {"scope": "repo", "keys": ["platform", "org", "full_name"], "desc": "GitHub Pages sites, custom domains & HTTPS enforcement"},
    # Engineering Metrics & DORA
    "pull_request_metrics": {"scope": "repo", "keys": ["platform", "org", "repo", "pr_id"], "desc": "PR inventory, merge status & cycle time hours (supports backfill)"},
    "pr_reviews": {"scope": "repo", "keys": ["platform", "org", "repo", "pr_id"], "desc": "PR time-to-first-review, approval counts & reviewers"},
    "contributors_bus_factor": {"scope": "repo", "keys": ["platform", "org", "full_name"], "desc": "Top contributor commit share & bus-factor risk rating"},
    "issues_sla": {"scope": "repo", "keys": ["platform", "org", "repo", "issue_number"], "desc": "Issue backlog, labels & time-to-close SLA hours"},
    "dora_metrics": {"scope": "repo", "keys": ["platform", "org", "full_name"], "desc": "Deployments, releases, merged PRs & p95 lead time per repo"},
    # Billing & Copilot
    "billing_copilot": {"scope": "org", "keys": ["platform", "org", "category", "assignee", "repo"], "desc": "Actions/Packages/Storage billing, GHAS seats, Copilot seats & Codespaces"},
    # Universal Escape Hatches
    "raw": {"scope": "dynamic", "keys": ["platform", "org", "repo", "id", "name"], "desc": "Any REST endpoint via --endpoint + --raw-scope org|repo|enterprise"},
    "graphql": {"scope": "org", "keys": ["platform", "org"], "desc": "Any GraphQL query via --query or --query-file (ProjectsV2, IP allow lists, etc.)"},
}

CORE_REPORTS = [
    "ownership_baseline",
    "apps_service_accounts",
    "archived_inactive",
    "repo_audit",
    "repo_configurations",
]

EXTENDED_REPORTS = [k for k in REPORT_CATALOG if k not in ("raw", "graphql")]


def chunked(items: List[Any], size: int) -> Iterable[List[Any]]:
    step = max(1, size)
    for i in range(0, len(items), step):
        yield items[i : i + step]


def run_chunked(
    label: str,
    items: List[Dict[str, Any]],
    worker_fn: Callable[[Dict[str, Any]], Any],
    workers: int,
    chunk_size: int,
    http_client: Any,
    state_db: Optional[DBManager] = None,
    run_key: Optional[str] = None,
    resume: bool = False,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []

    completed_map: Dict[str, List[Dict[str, Any]]] = {}
    if resume and state_db and run_key:
        completed_map = state_db.get_completed_checkpoints(run_key)
        if completed_map:
            print(f"[resume] Restored {len(completed_map)} completed items for {run_key}")
            for saved in completed_map.values():
                results.extend(saved)

    pending = [i for i in items if str(i.get("full_name", i)) not in completed_map]
    batches = list(chunked(pending, chunk_size))
    tracker = ProgressTracker(label, total=len(pending), total_chunks=len(batches))

    for b_idx, batch in enumerate(batches, start=1):
        tracker.set_chunk(b_idx)
        batch_cp: Dict[str, List[Dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            fut_map = {pool.submit(worker_fn, item): item for item in batch}
            for fut in as_completed(fut_map):
                item = fut_map[fut]
                item_id = str(item.get("full_name", item))
                try:
                    res = fut.result()
                    item_rows = res if isinstance(res, list) else ([res] if res is not None else [])
                    results.extend(item_rows)
                    batch_cp[item_id] = item_rows
                    tracker.advance(
                        1,
                        error=False,
                        req_count=getattr(http_client, "total_requests", 0),
                        rate_remaining=getattr(http_client, "last_rate_remaining", "?"),
                    )
                except Exception as exc:
                    errors.append({"item": item_id, "error": str(exc)})
                    tracker.advance(
                        1,
                        error=True,
                        req_count=getattr(http_client, "total_requests", 0),
                        rate_remaining=getattr(http_client, "last_rate_remaining", "?"),
                    )
        if state_db and run_key and batch_cp:
            state_db.save_checkpoint_batch(run_key, batch_cp)

    tracker.finish(
        req_count=getattr(http_client, "total_requests", 0),
        rate_remaining=getattr(http_client, "last_rate_remaining", "?"),
    )
    return results, errors


def build_summary_rollup(report_type: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not rows:
        return []
    if report_type == "ownership_baseline":
        buckets = defaultdict(lambda: {"total_active": 0, "has_owner": 0, "has_ci": 0, "both": 0})
        for r in rows:
            area = r.get("ext_business_area") or r.get("org") or "Unassigned"
            has_owner = bool(r.get("has_owner_signal") or r.get("ext_owner"))
            has_ci = bool(r.get("has_ci"))
            b = buckets[area]
            b["total_active"] += 1
            b["has_owner"] += int(has_owner)
            b["has_ci"] += int(has_ci)
            b["both"] += int(has_owner and has_ci)
        return [{"group": k, **v} for k, v in sorted(buckets.items())]

    if report_type == "apps_service_accounts":
        buckets = defaultdict(
            lambda: {
                "total": 0,
                "with_registered_owner": 0,
                "with_installer_from_audit": 0,
                "with_business_owner": 0,
                "missing_business_owner": 0,
            }
        )
        for r in rows:
            key = (r.get("org", ""), r.get("record_type", ""))
            b = buckets[key]
            b["total"] += 1
            reg = str(r.get("registered_app_owner") or "")
            if reg and reg != "PRIVATE_OR_INTERNAL_APP":
                b["with_registered_owner"] += 1
            if r.get("installed_by_audit_log"):
                b["with_installer_from_audit"] += 1
            biz = r.get("business_owner") or r.get("ext_owner") or r.get("ext_business_owner")
            if biz:
                b["with_business_owner"] += 1
            else:
                b["missing_business_owner"] += 1
        return [{"org": k[0], "record_type": k[1], **v} for k, v in sorted(buckets.items())]

    if report_type == "archived_inactive":
        buckets = defaultdict(lambda: {"repo_count": 0, "total_size_mb": 0.0})
        for r in rows:
            key = (r.get("org", ""), r.get("status", ""), r.get("proposed_disposition", ""))
            buckets[key]["repo_count"] += 1
            buckets[key]["total_size_mb"] = round(buckets[key]["total_size_mb"] + float(r.get("size_mb") or 0), 2)
        return [{"org": k[0], "status": k[1], "proposed_disposition": k[2], **v} for k, v in sorted(buckets.items())]

    if report_type == "repo_audit":
        buckets = defaultdict(
            lambda: {
                "repos": 0,
                "total_size_mb": 0.0,
                "lfs_enabled": 0,
                "actions_enabled": 0,
                "with_secrets": 0,
                "with_branch_protection": 0,
                "with_direct_assignments": 0,
            }
        )
        for r in rows:
            b = buckets[r.get("org", "")]
            b["repos"] += 1
            b["total_size_mb"] = round(b["total_size_mb"] + float(r.get("size_mb") or 0), 2)
            b["lfs_enabled"] += int(bool(r.get("lfs_enabled")))
            b["actions_enabled"] += int(r.get("actions_enabled") is True)
            b["with_secrets"] += int(isinstance(r.get("repo_secrets_count"), int) and r["repo_secrets_count"] > 0)
            b["with_branch_protection"] += int(bool(r.get("has_branch_protection_or_ruleset")))
            b["with_direct_assignments"] += int(isinstance(r.get("direct_assignments_count"), int) and r["direct_assignments_count"] > 0)
        return [{"org": k, **v} for k, v in sorted(buckets.items())]

    if report_type == "enterprise_policies":
        buckets = defaultdict(int)
        for r in rows:
            buckets[(r.get("enterprise", ""), r.get("category", ""), r.get("status", ""))] += 1
        return [{"enterprise": k[0], "category": k[1], "status": k[2], "policy_count": v} for k, v in sorted(buckets.items())]

    if report_type == "security_alerts":
        buckets = defaultdict(int)
        for r in rows:
            buckets[(r.get("org", ""), r.get("alert_type", ""), r.get("severity", "unknown"))] += 1
        return [{"org": k[0], "alert_type": k[1], "severity": k[2], "open_alerts": v} for k, v in sorted(buckets.items())]

    if report_type == "actions_pinning":
        buckets = defaultdict(lambda: {"total_uses": 0, "pinned": 0, "unpinned": 0})
        for r in rows:
            b = buckets[r.get("org", "")]
            b["total_uses"] += 1
            b["pinned" if r.get("pinned_to_sha") else "unpinned"] += 1
        return [{"org": k, **v} for k, v in sorted(buckets.items())]

    if report_type == "contributors_bus_factor":
        buckets = defaultdict(int)
        for r in rows:
            buckets[(r.get("org", ""), r.get("bus_factor_risk", "unknown"))] += 1
        return [{"org": k[0], "bus_factor_risk": k[1], "repo_count": v} for k, v in sorted(buckets.items())]

    return []


def run_report(
    clients: List[VCSClient],
    report_type: str,
    repo_cache: Dict[str, List[Dict[str, Any]]],
    workers: int = 8,
    chunk_size: int = 50,
    inactive_days: int = 180,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: Optional[int] = None,
    repo_filter: Optional[str] = None,
    skip_archived: bool = False,
    only_items: Optional[Set[str]] = None,
    raw_endpoint: Optional[str] = None,
    raw_scope: str = "org",
    raw_params: Optional[Dict[str, Any]] = None,
    extract_key: Optional[str] = None,
    graphql_query: Optional[str] = None,
    graphql_vars: Optional[Dict[str, Any]] = None,
    state_db: Optional[DBManager] = None,
    resume: bool = False,
    dry_run: bool = False,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]], List[str]]:
    if report_type not in REPORT_CATALOG:
        raise ValueError(f"Unknown report '{report_type}'. Run 'datascrape list-reports' to view all options.")

    all_rows: List[Dict[str, Any]] = []
    all_errors: List[Dict[str, str]] = []
    regex = re.compile(repo_filter, re.IGNORECASE) if repo_filter else None
    seen_enterprises: Set[str] = set()

    for idx, client in enumerate(clients):
        cache_key = f"{client.platform}:{client.base_url}:{client.org_or_project}"
        run_key = f"{cache_key}:{report_type}"
        print(f"\n[run] {client.platform} ({client.deployment_type}) -> {client.org_or_project} [{report_type}]")

        # Enterprise-scoped raw query runs once per unique enterprise/server
        if report_type == "raw" and raw_scope == "enterprise":
            ent_key = f"{client.platform}:{client.base_url}:{client.enterprise}"
            if ent_key in seen_enterprises:
                continue
            seen_enterprises.add(ent_key)
            if dry_run:
                print(f"[dry-run] Would query enterprise endpoint: {raw_endpoint}")
                continue
            all_rows.extend(client.raw_query(raw_endpoint or "", params=raw_params, extract_key=extract_key))
            continue

        # Enterprise-layer policies & configurations (runs once per unique enterprise/server)
        if report_type == "enterprise_policies":
            ent_key = f"{client.platform}:{client.base_url}:{client.enterprise}"
            if ent_key in seen_enterprises:
                continue
            seen_enterprises.add(ent_key)
            if dry_run:
                print(f"[dry-run] Would audit enterprise policies for '{client.enterprise or client.base_url}'")
                continue
            all_rows.extend(client.audit_enterprise_policies())
            continue

        # Org-scoped reports
        if report_type == "graphql":
            if dry_run:
                print(f"[dry-run] Would execute GraphQL query for {client.org_or_project}")
                continue
            all_rows.extend(client.graphql_query(graphql_query or "", variables=graphql_vars))
            continue

        if report_type == "raw" and raw_scope == "org":
            if dry_run:
                print(f"[dry-run] Would query org endpoint: {raw_endpoint}")
                continue
            all_rows.extend(client.raw_query(raw_endpoint or "", params=raw_params, extract_key=extract_key))
            continue

        org_dispatch = {
            "apps_service_accounts": lambda c: c.list_apps_and_service_accounts(),
            "teams_members": lambda c: c.list_teams_members(),
            "iam_tokens_sso": lambda c: c.list_iam_tokens_sso(),
            "dormant_users": lambda c: c.list_dormant_users(inactive_days=inactive_days),
            "security_alerts": lambda c: c.list_security_alerts(),
            "org_secrets_variables": lambda c: c.list_org_secrets_variables(),
            "audit_log": lambda c: c.list_audit_log(since=since, until=until),
            "runners_packages": lambda c: c.list_runners_packages(),
            "billing_copilot": lambda c: c.list_billing_copilot(),
        }
        if report_type in org_dispatch:
            if dry_run:
                print(f"[dry-run] Would execute org-level report '{report_type}' on {client.org_or_project}")
                continue
            all_rows.extend(org_dispatch[report_type](client))
            continue

        # Repo-scoped reports (cached repo discovery)
        if cache_key not in repo_cache:
            repo_cache[cache_key] = client.list_repos()
        repos = list(repo_cache[cache_key])
        if skip_archived:
            repos = [r for r in repos if not r.get("archived")]
        if regex:
            repos = [r for r in repos if regex.search(r["name"]) or regex.search(r["full_name"])]
        if only_items:
            repos = [r for r in repos if r["full_name"] in only_items or r["name"] in only_items]
        if limit is not None and limit > 0:
            repos = repos[:limit]

        if dry_run:
            print(f"[dry-run] Would process {len(repos)} repos for '{report_type}' (workers={workers}, chunk={chunk_size})")
            continue

        if report_type == "archived_inactive":
            now = datetime.now(timezone.utc)
            for r in repos:
                days = None
                if r.get("pushed_at"):
                    dt = datetime.fromisoformat(r["pushed_at"].replace("Z", "+00:00"))
                    days = (now - dt).days
                is_inactive = (not r["archived"]) and (days is None or days >= inactive_days)
                if not r["archived"] and not is_inactive:
                    continue
                status = "archived" if r["archived"] else f"inactive_{inactive_days}d+"
                disp = (
                    "Retain Archived / Audit for Purge"
                    if r["archived"]
                    else ("Propose Archive (12+ mo)" if (days or 9999) >= 365 else "Verify Ownership / Propose Archive")
                )
                all_rows.append({
                    **r,
                    "status": status,
                    "days_since_push": days if days is not None else "never",
                    "size_mb": round(r["size_kb"] / 1024, 2),
                    "proposed_disposition": disp,
                })
            continue

        repo_workers: Dict[str, Tuple[List[Dict[str, Any]], Callable[[Dict[str, Any]], Any]]] = {
            "ownership_baseline": ([r for r in repos if not r["archived"]], lambda r: {**r, **client.check_owner_and_ci(r)}),
            "repo_audit": (repos, client.audit_repo),
            "repo_configurations": (repos, client.audit_repo_configurations),
            "languages": (repos, client.list_languages),
            "releases_tags": (repos, client.list_releases_tags),
            "submodules": (repos, client.list_submodules),
            "webhooks_deploy_keys": (repos, client.list_webhooks_deploy_keys),
            "sbom_licenses": (repos, client.audit_sbom_licenses),
            "actions_pinning": (repos, client.audit_actions_pinning),
            "environments": (repos, client.list_environments),
            "workflow_runs": (repos, client.audit_workflow_runs),
            "pages": (repos, client.audit_pages),
            "pull_request_metrics": (repos, lambda r: client.list_pull_requests(r, since=since, until=until)),
            "pr_reviews": (repos, lambda r: client.audit_pr_reviews(r, since=since, until=until)),
            "contributors_bus_factor": (repos, client.audit_contributors_bus_factor),
            "issues_sla": (repos, lambda r: client.audit_issues_sla(r, since=since, until=until)),
            "dora_metrics": (repos, lambda r: client.audit_dora_metrics(r, since=since, until=until)),
            "raw": (repos, lambda r: client.raw_query(raw_endpoint or "", params=raw_params, repo=r, extract_key=extract_key)),
        }
        target_repos, worker_fn = repo_workers[report_type]
        rows, errs = run_chunked(
            f"{client.org_or_project}:{report_type}",
            target_repos,
            worker_fn,
            workers=workers,
            chunk_size=chunk_size,
            http_client=client.http,
            state_db=state_db,
            run_key=run_key,
            resume=resume,
        )
        all_rows.extend(rows)
        all_errors.extend(errs)

    return all_rows, all_errors, REPORT_CATALOG[report_type]["keys"]