import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Set, Union
from datascrape.clients import build_client
from datascrape.clients.base import VCSClient
from datascrape.reports import (
    CORE_REPORTS,
    EXTENDED_REPORTS,
    REPORT_CATALOG,
    build_summary_rollup,
    run_report,
)
from datascrape.storage import DBManager, enrich_rows, export_report, load_enrichment_csv


def _print_report_catalog() -> None:
    print(f"{'REPORT NAME':<25} {'SCOPE':<11} DESCRIPTION")
    print("-" * 97)
    for name, meta in REPORT_CATALOG.items():
        print(f"{name:<25} {meta['scope']:<11} {meta['desc']}")
    print("\nMeta shortcuts: --report all (5 core reports) | --report all_extended (all 28 built-in reports)")


def _run_diff_command(argv: List[str]) -> None:
    p = argparse.ArgumentParser(prog="datascrape diff", description="Compare two SQLite snapshots for a report")
    p.add_argument("--db-path", type=Path, required=True, help="SQLite DB path")
    p.add_argument("--report", required=True, help="Report type to diff")
    p.add_argument("--from-run", default=None, help="Starting snapshot run_id (defaults to 2nd newest)")
    p.add_argument("--to-run", default=None, help="Ending snapshot run_id (defaults to newest)")
    p.add_argument("--list-runs", action="store_true", help="List available snapshot run_ids and exit")
    p.add_argument("--output", default="csv,json", help="csv,json | csv | json")
    p.add_argument("--out-dir", type=Path, default=Path("./reports"))
    args = p.parse_args(argv)

    db = DBManager(args.db_path)
    try:
        runs = db.list_runs(args.report)
        if args.list_runs:
            print(f"Snapshots for '{args.report}':")
            for run_id, cnt in runs:
                print(f"  - {run_id} ({cnt} rows)")
            return
        if len(runs) < 2 and (not args.from_run or not args.to_run):
            raise SystemExit(f"Need at least 2 snapshots for '{args.report}' to diff (found {len(runs)}).")
        from_run = args.from_run or runs[-2][0]
        to_run = args.to_run or runs[-1][0]
        changes = db.diff_snapshots(args.report, from_run, to_run)
        formats = [f.strip().lower() for f in args.output.split(",") if f.strip()]
        export_report(changes, args.out_dir, f"diff_{args.report}_{from_run}_to_{to_run}", formats)
        print(f"[diff] Found {len(changes)} changes between {from_run} and {to_run}")
    finally:
        db.close()


def _parse_kv_params(items: Optional[List[str]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for pair in items or []:
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _load_retry_items(errors_csv: Optional[Path]) -> Optional[Set[str]]:
    if not errors_csv:
        return None
    items: Set[str] = set()
    with errors_csv.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("item"):
                items.add(row["item"].strip())
    return items


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("list-reports", "list"):
        _print_report_catalog()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "diff":
        _run_diff_command(sys.argv[2:])
        return

    parser = argparse.ArgumentParser(
        prog="datascrape",
        description="Universal multi-VCS concurrent audit, security, and observability CLI (run 'datascrape list-reports' or 'datascrape diff --help')",
    )
    parser.add_argument("target", help="github | ghes | bitbucket | combined")
    parser.add_argument("--type", dest="deployment_type", choices=["cloud", "self_hosted"], default="cloud")
    parser.add_argument("--url", default="https://api.github.com", help="Base URL for cloud or self-hosted server")
    parser.add_argument("--token", default="", help="API token or ENV:VAR_NAME")
    parser.add_argument("--auth-type", choices=["bearer", "basic"], default="bearer", help="Use basic for Bitbucket user:app_password")
    parser.add_argument("--org", default="", help="Org / Workspace / Project (comma-separated supported)")
    parser.add_argument("--enterprise", default="", help="Enterprise slug (e.g. my-corp) for enterprise-layer reports")
    parser.add_argument("--all-orgs", action="store_true", help="Auto-discover all accessible orgs/workspaces/projects")
    parser.add_argument("--exclude-orgs", default="", help="Comma-separated orgs to skip")
    parser.add_argument("--config", type=Path, default=None, help="JSON config file for combined multi-client runs")

    # Reports & Escape Hatches
    parser.add_argument("--report", required=True, help="Report name(s), comma-separated, 'all', 'all_extended', 'raw', or 'graphql'")
    parser.add_argument("--endpoint", default=None, help="REST path for --report raw (supports {org}, {enterprise}, {repo}, {full_name})")
    parser.add_argument("--raw-scope", choices=["org", "repo", "enterprise"], default="org", help="Scope for --report raw")
    parser.add_argument("--param", action="append", default=None, help="Extra query param for raw mode (key=val, repeatable)")
    parser.add_argument("--extract-key", default=None, help="JSON key to unwrap from raw dict responses (e.g. runners)")
    parser.add_argument("--query", default=None, help="Inline GraphQL query string for --report graphql")
    parser.add_argument("--query-file", type=Path, default=None, help="File containing GraphQL query for --report graphql")
    parser.add_argument("--vars", default=None, help="JSON string of variables for --report graphql")

    # Output & Customization
    parser.add_argument("--output", default="csv,json", help="Comma-delimited: csv,json | csv | json")
    parser.add_argument("--out-dir", type=Path, default=Path("./reports"))
    parser.add_argument("--columns", default=None, help="Comma-separated columns to keep/order in output")
    parser.add_argument("--enrich-csv", type=Path, default=None, help="External CSV (IMS/CMDB/app owners) to join onto results")
    parser.add_argument("--enrich-key", default="repo_name", help="Join column name inside --enrich-csv")
    parser.add_argument("--service-pattern", default=None, help="Custom regex for detecting service/bot accounts")
    parser.add_argument("--custom-ci-files", default="", help="Comma-separated extra CI file paths to check")

    # Concurrency, Filtering & Execution Control
    parser.add_argument("--workers", type=int, default=8, help="Concurrent worker threads")
    parser.add_argument("--chunk-size", type=int, default=50, help="Items per chunk")
    parser.add_argument("--limit", type=int, default=None, help="Test batch: process only first N repos per org")
    parser.add_argument("--repo-filter", default=None, help="Regex filter for repo names")
    parser.add_argument("--skip-archived", action="store_true", help="Skip archived repositories")
    parser.add_argument("--inactive-days", type=int, default=180, help="Inactivity threshold in days")
    parser.add_argument("--dry-run", action="store_true", help="Preview discovered orgs and repo counts without deep API calls")

    # Database, Backfill, Resume & Retry
    parser.add_argument("--db-path", type=Path, default=None, help="SQLite DB path for upsert + historical snapshots")
    parser.add_argument("--backfill-since", default=None, help="ISO-8601 start date for PR/issue/audit/DORA backfill")
    parser.add_argument("--backfill-until", default=None, help="ISO-8601 end date for backfill")
    parser.add_argument("--resume", action="store_true", help="Resume from last saved chunk checkpoint")
    parser.add_argument("--retry-errors", type=Path, default=None, help="Re-run only failed items from an errors CSV")

    # Network & Reliability Tuning
    parser.add_argument("--max-retries", type=int, default=6, help="Max retry attempts per request")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds")
    parser.add_argument("--rate-buffer", type=int, default=10, help="Pause when remaining API quota hits this buffer")
    parser.add_argument("--ca-bundle", type=str, default=None, help="Path to corporate root CA bundle (.pem/.crt)")
    parser.add_argument("--insecure", action="store_true", help="Disable SSL certificate verification")

    args = parser.parse_args()
    verify_ssl: Union[bool, str] = False if args.insecure else (args.ca_bundle if args.ca_bundle else True)
    formats = [f.strip().lower() for f in args.output.split(",") if f.strip()]
    excluded = {o.strip().lower() for o in args.exclude_orgs.split(",") if o.strip()}
    columns = [c.strip() for c in args.columns.split(",") if c.strip()] if args.columns else None
    custom_ci = [c.strip() for c in args.custom_ci_files.split(",") if c.strip()]
    raw_params = _parse_kv_params(args.param)

    gql_text = args.query_file.read_text(encoding="utf-8") if args.query_file else args.query
    gql_vars = json.loads(args.vars) if args.vars else None

    req_reports: List[str] = []
    for r in [x.strip().lower() for x in args.report.split(",") if x.strip()]:
        if r == "all":
            req_reports.extend(CORE_REPORTS)
        elif r == "all_extended":
            req_reports.extend(EXTENDED_REPORTS)
        else:
            req_reports.append(r)
    req_reports = list(dict.fromkeys(req_reports))

    enterprise_only = all(
        r == "enterprise_policies" or (r == "raw" and args.raw_scope == "enterprise")
        for r in req_reports
    )

    client_opts: Dict[str, Any] = dict(
        verify_ssl=verify_ssl,
        max_retries=args.max_retries,
        timeout=args.timeout,
        rate_buffer=args.rate_buffer,
        service_pattern=args.service_pattern,
        custom_ci_files=custom_ci,
        enterprise=args.enterprise,
    )

    clients: List[VCSClient] = []
    if args.target.lower() == "combined":
        if not args.config:
            parser.error("--config is required when target is 'combined'")
        for entry in json.loads(args.config.read_text(encoding="utf-8")):
            auth_type = entry.get("auth_type", args.auth_type)
            entry_opts = {**client_opts, "enterprise": entry.get("enterprise", args.enterprise)}
            if entry.get("all_orgs"):
                seed = build_client(entry["target"], entry["type"], entry.get("url", ""), entry["token"], "", auth_type=auth_type, **entry_opts)
                orgs = [o for o in seed.list_orgs() if o.lower() not in excluded]
            else:
                orgs = [o.strip() for o in str(entry.get("org", "")).split(",") if o.strip() and o.strip().lower() not in excluded]
            if not orgs and enterprise_only:
                orgs = [entry.get("enterprise") or "site"]
            for org in orgs:
                clients.append(build_client(entry["target"], entry["type"], entry.get("url", ""), entry["token"], org, auth_type=auth_type, **entry_opts))
    else:
        if args.all_orgs:
            seed = build_client(args.target, args.deployment_type, args.url, args.token, "", auth_type=args.auth_type, **client_opts)
            orgs = [o for o in seed.list_orgs() if o.lower() not in excluded]
            print(f"[discover] Found {len(orgs)} orgs: {', '.join(orgs)}")
        elif enterprise_only:
            orgs = [args.org or args.enterprise or "site"]
        else:
            orgs = [o.strip() for o in args.org.split(",") if o.strip() and o.strip().lower() not in excluded]
        if not orgs:
            parser.error("Provide --org <name>, --enterprise <slug>, or use --all-orgs")
        for org in orgs:
            clients.append(build_client(args.target, args.deployment_type, args.url, args.token, org, auth_type=args.auth_type, **client_opts))

    enrichment = load_enrichment_csv(args.enrich_csv, args.enrich_key) if args.enrich_csv else {}
    only_items = _load_retry_items(args.retry_errors)
    state_db_path = args.db_path or (args.out_dir / ".datascrape_checkpoints.db" if args.resume else None)
    db = DBManager(state_db_path) if state_db_path else None
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    repo_cache: Dict[str, List[Dict]] = {}

    try:
        for report_type in req_reports:
            rows, errors, key_fields = run_report(
                clients=clients,
                report_type=report_type,
                repo_cache=repo_cache,
                workers=args.workers,
                chunk_size=args.chunk_size,
                inactive_days=args.inactive_days,
                since=args.backfill_since,
                until=args.backfill_until,
                limit=args.limit,
                repo_filter=args.repo_filter,
                skip_archived=args.skip_archived,
                only_items=only_items,
                raw_endpoint=args.endpoint,
                raw_scope=args.raw_scope,
                raw_params=raw_params,
                extract_key=args.extract_key,
                graphql_query=gql_text,
                graphql_vars=gql_vars,
                state_db=db,
                resume=args.resume,
                dry_run=args.dry_run,
            )
            if args.dry_run:
                continue

            if enrichment:
                rows = enrich_rows(rows, enrichment, ["full_name", "repo", "name", "identifier"])

            stem = f"{args.target}_{report_type}"
            export_report(rows, args.out_dir, stem, formats, columns=columns)

            rollup = build_summary_rollup(report_type, rows)
            if rollup:
                export_report(rollup, args.out_dir, f"{stem}_summary", formats)

            if errors:
                export_report(errors, args.out_dir, f"{stem}_errors", ["csv", "json"])

            if args.db_path and db:
                upserted = db.upsert_rows(report_type, rows, key_fields, run_id=run_id)
                print(f"[db] Upserted {upserted} rows (snapshot {run_id}) -> {args.db_path}")
    finally:
        if db:
            db.close()


if __name__ == "__main__":
    main()