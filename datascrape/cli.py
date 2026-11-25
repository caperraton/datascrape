"""Command-line interface for Datascrape."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

from datascrape.clients import build_client
from datascrape.clients.base import VCSClient
from datascrape.logging_utils import configure_logging, prune_files
from datascrape.reports import (
    CORE_REPORTS,
    EXTENDED_REPORTS,
    REPORT_CATALOG,
    build_summary_rollup,
    run_report,
)
from datascrape.run_metadata import write_run_manifest
from datascrape.shutdown import clear_shutdown, request_shutdown
from datascrape.storage import DBManager, enrich_rows, export_report, export_run_dashboard, load_enrichment_csv, redact_sensitive_rows

logger = logging.getLogger(__name__)


def _print_report_catalog() -> None:
    print(f"{'REPORT NAME':<25} {'SCOPE':<11} DESCRIPTION")
    print("-" * 97)
    for name, meta in REPORT_CATALOG.items():
        print(f"{name:<25} {meta['scope']:<11} {meta['desc']}")
    print("\nMeta shortcuts: --report all (5 core reports) | --report all_extended (all 28 built-in reports)")


def _run_diff_command(argv: List[str]) -> None:
    parser = argparse.ArgumentParser(prog="datascrape diff", description="Compare two SQLite snapshots for a report")
    parser.add_argument("--db-path", type=Path, required=True, help="SQLite DB path")
    parser.add_argument("--report", required=True, help="Report type to diff")
    parser.add_argument("--from-run", default=None, help="Starting snapshot run_id (defaults to 2nd newest)")
    parser.add_argument("--to-run", default=None, help="Ending snapshot run_id (defaults to newest)")
    parser.add_argument("--list-runs", action="store_true", help="List available snapshot run_ids and exit")
    parser.add_argument("--output", default="csv,json", help="csv,json,xlsx,html | csv | json")
    parser.add_argument("--out-dir", type=Path, default=Path("./reports"))
    args = parser.parse_args(argv)

    db = DBManager(args.db_path)
    try:
        db.integrity_check()
        runs = db.list_runs(args.report)
        if args.list_runs:
            print(f"Snapshots for '{args.report}':")
            for run_id, count in runs:
                print(f"  - {run_id} ({count} rows)")
            return
        if len(runs) < 2 and (not args.from_run or not args.to_run):
            raise SystemExit(f"Need at least 2 snapshots for '{args.report}' to diff (found {len(runs)}).")
        from_run = args.from_run or runs[-2][0]
        to_run = args.to_run or runs[-1][0]
        changes = db.diff_snapshots(args.report, from_run, to_run)
        formats = [item.strip().lower() for item in args.output.split(",") if item.strip()]
        invalid = sorted(set(formats) - {"csv", "json", "xlsx", "html"})
        if not formats or invalid:
            parser.error(f"Unsupported diff output format(s): {', '.join(invalid) if invalid else args.output}")
        export_report(changes, args.out_dir, f"diff_{args.report}_{from_run}_to_{to_run}", formats)
        logger.info("Snapshot diff completed: %s changes between %s and %s", len(changes), from_run, to_run)
    finally:
        db.close()


def _run_trends_command(argv: List[str]) -> None:
    parser = argparse.ArgumentParser(prog="datascrape trends", description="Export historical snapshot counts for a report")
    parser.add_argument("--db-path", type=Path, required=True, help="SQLite DB path")
    parser.add_argument("--report", required=True, help="Report type to trend")
    parser.add_argument("--output", default="csv,json", help="csv,json,xlsx,html")
    parser.add_argument("--out-dir", type=Path, default=Path("./reports"))
    args = parser.parse_args(argv)
    db = DBManager(args.db_path)
    try:
        db.integrity_check()
        trends = db.report_trends(args.report)
        if not trends:
            logger.warning("No historical snapshots found for report %s", args.report)
        formats = [item.strip().lower() for item in args.output.split(",") if item.strip()]
        invalid = sorted(set(formats) - {"csv", "json", "xlsx", "html"})
        if not formats or invalid:
            parser.error(f"Unsupported trend output format(s): {', '.join(invalid) if invalid else args.output}")
        export_report(trends, args.out_dir, f"trends_{args.report}", formats)
        logger.info("Exported %s historical trend points for %s", len(trends), args.report)
    finally:
        db.close()


def _parse_kv_params(items: Optional[List[str]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for pair in items or []:
        if "=" in pair:
            key, value = pair.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def _load_retry_items(errors_csv: Optional[Path]) -> Optional[Set[str]]:
    if not errors_csv:
        return None
    items: Set[str] = set()
    with errors_csv.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if row.get("item"):
                items.add(row["item"].strip())
    logger.info("Loaded %s failed repository identifiers from %s", len(items), errors_csv)
    return items


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="datascrape",
        description="Universal multi-VCS audit and observability CLI. Use 'datascrape list-reports' or 'datascrape diff --help'.",
    )
    parser.add_argument("target", choices=["github", "ghes", "bitbucket", "combined"], help="Source-control platform")
    parser.add_argument("--type", dest="deployment_type", choices=["cloud", "self_hosted"], default="cloud")
    parser.add_argument("--url", default="https://api.github.com", help="Base URL for cloud or self-hosted server")
    parser.add_argument("--token", default="", help="API token or ENV:VAR_NAME; GitHub App auth does not require this")
    parser.add_argument("--auth-type", choices=["bearer", "basic", "github_app"], default="bearer")
    parser.add_argument("--app-id", default=None, help="GitHub App ID (for --auth-type github_app)")
    parser.add_argument("--installation-id", default=None, help="GitHub App installation ID")
    parser.add_argument("--private-key", default=None, help="GitHub App PEM file path or ENV:VAR_NAME containing the PEM")
    parser.add_argument("--org", default="", help="Org / Workspace / Project (comma-separated supported)")
    parser.add_argument("--enterprise", default="", help="Enterprise slug for enterprise-layer reports")
    parser.add_argument("--all-orgs", action="store_true", help="Auto-discover all accessible orgs/workspaces/projects")
    parser.add_argument("--exclude-orgs", default="", help="Comma-separated orgs to skip")
    parser.add_argument("--config", type=Path, default=None, help="JSON config file for combined multi-client runs")

    parser.add_argument("--report", required=True, help="Report name(s), comma-separated, 'all', 'all_extended', 'raw', or 'graphql'")
    parser.add_argument("--endpoint", default=None, help="REST path for --report raw")
    parser.add_argument("--raw-scope", choices=["org", "repo", "enterprise"], default="org")
    parser.add_argument("--param", action="append", default=None, help="Extra query param for raw mode (key=val, repeatable)")
    parser.add_argument("--extract-key", default=None, help="JSON key to unwrap from raw dict responses")
    parser.add_argument("--query", default=None, help="Inline GraphQL query string")
    parser.add_argument("--query-file", type=Path, default=None, help="File containing GraphQL query")
    parser.add_argument("--vars", default=None, help="JSON string of GraphQL variables")

    parser.add_argument("--output", default="csv,json", help="Comma-delimited: csv,json,xlsx,html")
    parser.add_argument("--out-dir", type=Path, default=Path("./reports"))
    parser.add_argument("--columns", default=None, help="Comma-separated columns to keep/order in output")
    parser.add_argument("--enrich-csv", type=Path, default=None, help="External CSV to join onto results")
    parser.add_argument("--enrich-key", default="repo_name", help="Join column name inside --enrich-csv")
    parser.add_argument("--service-pattern", default=None, help="Custom regex for detecting service/bot accounts")
    parser.add_argument("--custom-ci-files", default="", help="Comma-separated extra CI file paths to check")
    parser.add_argument("--redact-sensitive", action="store_true", help="Redact sensitive fields in exported and persisted report rows")
    parser.add_argument("--classification", choices=["public", "internal", "confidential", "restricted"], default="confidential", help="Data classification recorded in the run manifest")

    parser.add_argument("--batch", "--chunk-size", dest="batch_size", type=int, default=50, help="Repositories per batch (alias: --chunk-size)")
    parser.add_argument("--workers", "--threads", dest="workers", type=int, default=8, help="Concurrent worker threads (alias: --threads)")
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N repositories per org")
    parser.add_argument("--repo-filter", default=None, help="Regex filter for repository names")
    parser.add_argument("--skip-archived", action="store_true", help="Skip archived repositories")
    parser.add_argument("--inactive-days", type=int, default=180, help="Inactivity threshold in days")
    parser.add_argument("--dry-run", action="store_true", help="Preview work without deep API calls")

    parser.add_argument("--db-path", type=Path, default=None, help="SQLite DB path for upserts, snapshots and checkpoints")
    parser.add_argument("--db-backup", action="store_true", help="Create a timestamped SQLite backup after a successful run")
    parser.add_argument("--db-backup-dir", type=Path, default=Path("./backups"))
    parser.add_argument("--db-retention-days", type=int, default=None, help="Prune historical snapshots/checkpoints older than this many days")
    parser.add_argument("--backfill-since", default=None, help="ISO-8601 start date for supported backfills")
    parser.add_argument("--backfill-until", default=None, help="ISO-8601 end date for supported backfills")
    parser.add_argument("--resume", action="store_true", help="Resume from saved chunk checkpoints")
    parser.add_argument("--retry-errors", type=Path, default=None, help="Re-run only failed items listed in an errors CSV")

    parser.add_argument("--max-retries", type=int, default=8, help="Maximum retries per HTTP request")
    parser.add_argument("--backoff-base", type=float, default=2.0, help="Initial exponential backoff delay in seconds")
    parser.add_argument("--backoff-max", type=float, default=900.0, help="Maximum exponential backoff delay in seconds; server reset times take precedence")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds")
    parser.add_argument("--rate-buffer", type=int, default=10, help="Pause before the API quota reaches this remaining-request threshold")
    parser.add_argument("--rate-check", action=argparse.BooleanOptionalAction, default=True, help="Check GitHub API quota before scanning (default enabled)")
    parser.add_argument("--preflight", action=argparse.BooleanOptionalAction, default=True, help="Check GitHub organization visibility before scanning")
    parser.add_argument("--ca-bundle", type=str, default=None, help="Path to corporate root CA bundle (.pem/.crt)")
    parser.add_argument("--insecure", action="store_true", help="Disable SSL verification; avoid unless absolutely necessary")

    # Parsed in a bootstrap parser so they also work with list-reports and diff.
    parser.add_argument("--log-dir", type=Path, default=Path("./logs"), help="Directory for timestamped run logs")
    parser.add_argument("--log-level", default=os.environ.get("DATASCRAPE_LOG_LEVEL", "INFO"), help="DEBUG, INFO, WARNING, ERROR, or CRITICAL")
    parser.add_argument("--log-file", default=None, help="Override the generated timestamped log filename")
    parser.add_argument("--no-file-log", action="store_true", help="Disable file logging (console logging remains enabled)")
    parser.add_argument("--verbose", action="store_true", help="Shortcut for --log-level DEBUG")
    parser.add_argument("--log-retention-days", type=int, default=None, help="Delete generated run logs older than this many days")
    parser.add_argument("--run-dir", type=Path, default=Path("./runs"), help="Directory for JSON run manifests")
    parser.add_argument("--run-retention-days", type=int, default=None, help="Delete run manifests older than this many days")
    parser.add_argument("--report-retention-days", type=int, default=None, help="Delete report exports older than this many days")
    return parser


def _preflight_client(client: VCSClient, checked_rate_limits: Set[str], rate_check: bool, preflight: bool) -> None:
    if rate_check and hasattr(client, "check_rate_limit"):
        coordinator_key = getattr(client.http, "coordinator_key", client.base_url)
        if coordinator_key not in checked_rate_limits:
            client.check_rate_limit()  # type: ignore[attr-defined]
            checked_rate_limits.add(coordinator_key)
    if preflight and hasattr(client, "check_org_access"):
        client.check_org_access()  # type: ignore[attr-defined]


def _validate_formats(parser: argparse.ArgumentParser, output: str) -> List[str]:
    formats = [item.strip().lower() for item in output.split(",") if item.strip()]
    supported = {"csv", "json", "xlsx", "html"}
    invalid = sorted(set(formats) - supported)
    if not formats or invalid:
        parser.error(f"Unsupported output format(s): {', '.join(invalid) if invalid else output}. Choose csv,json,xlsx,html.")
    return list(dict.fromkeys(formats))


def _handle_termination(signum: int, frame: Any) -> None:
    logger.warning("Received signal %s; requesting a graceful stop at the current batch boundary", signum)
    request_shutdown()
    raise KeyboardInterrupt(f"Received signal {signum}")


def main(argv: Optional[List[str]] = None) -> None:
    clear_shutdown()
    try:
        signal.signal(signal.SIGTERM, _handle_termination)
    except ValueError:  # pragma: no cover - main() may be invoked from a non-main thread
        pass
    argv = list(sys.argv[1:] if argv is None else argv)
    log_bootstrap = argparse.ArgumentParser(add_help=False)
    log_bootstrap.add_argument("--log-dir", type=Path, default=Path("./logs"))
    log_bootstrap.add_argument("--log-level", default=os.environ.get("DATASCRAPE_LOG_LEVEL", "INFO"))
    log_bootstrap.add_argument("--log-file", default=None)
    log_bootstrap.add_argument("--no-file-log", action="store_true")
    log_bootstrap.add_argument("--verbose", action="store_true")
    log_bootstrap.add_argument("--log-retention-days", type=int, default=None)
    log_args, remaining_argv = log_bootstrap.parse_known_args(argv)
    active_log = configure_logging(
        log_dir=log_args.log_dir,
        level="DEBUG" if log_args.verbose else log_args.log_level,
        log_file=log_args.log_file,
        file_logging=not log_args.no_file_log,
    )
    logger.info("Datascrape starting; log_file=%s", active_log or "disabled")
    removed_logs = prune_files(log_args.log_dir, "datascrape_*.log*", log_args.log_retention_days)
    if removed_logs:
        logger.info("Removed %s expired run log files", removed_logs)

    if remaining_argv and remaining_argv[0] in ("list-reports", "list"):
        _print_report_catalog()
        return
    if remaining_argv and remaining_argv[0] == "diff":
        _run_diff_command(remaining_argv[1:])
        return
    if remaining_argv and remaining_argv[0] == "trends":
        _run_trends_command(remaining_argv[1:])
        return

    parser = _make_parser()
    args = parser.parse_args(remaining_argv)
    # Restore logging settings parsed by the bootstrap parser for the run manifest.
    args.log_dir = log_args.log_dir
    args.log_level = "DEBUG" if log_args.verbose else log_args.log_level
    args.log_file = log_args.log_file
    args.no_file_log = log_args.no_file_log
    args.verbose = log_args.verbose
    args.log_retention_days = log_args.log_retention_days

    if args.batch_size < 1:
        parser.error("--batch must be at least 1")
    if args.workers < 1:
        parser.error("--workers/--threads must be at least 1")
    if args.max_retries < 0 or args.backoff_base <= 0 or args.backoff_max <= 0:
        parser.error("Retry count must be non-negative and backoff values must be positive")
    if args.timeout < 1 or args.rate_buffer < 0:
        parser.error("--timeout must be positive and --rate-buffer must be non-negative")
    if args.db_backup and not (args.db_path or args.resume):
        parser.error("--db-backup requires --db-path or --resume")
    if args.db_retention_days is not None and args.db_retention_days < 1:
        parser.error("--db-retention-days must be at least 1")
    if args.db_retention_days is not None and not (args.db_path or args.resume):
        parser.error("--db-retention-days requires --db-path or --resume")
    for retention_name in ("run_retention_days", "report_retention_days"):
        retention_value = getattr(args, retention_name)
        if retention_value is not None and retention_value < 1:
            parser.error(f"--{retention_name.replace('_', '-')} must be at least 1")
    if args.ca_bundle and not Path(args.ca_bundle).is_file():
        parser.error(f"CA bundle file not found: {args.ca_bundle}")
    if args.insecure:
        logger.warning("TLS certificate verification is disabled; use only when a trusted corporate CA bundle cannot be supplied")
    if args.auth_type == "github_app" and args.target != "combined" and not (args.app_id and args.installation_id and args.private_key):
        parser.error("--auth-type github_app requires --app-id, --installation-id, and --private-key")

    formats = _validate_formats(parser, args.output)
    excluded = {org.strip().lower() for org in args.exclude_orgs.split(",") if org.strip()}
    columns = [column.strip() for column in args.columns.split(",") if column.strip()] if args.columns else None
    custom_ci = [path.strip() for path in args.custom_ci_files.split(",") if path.strip()]
    raw_params = _parse_kv_params(args.param)
    gql_text = args.query_file.read_text(encoding="utf-8") if args.query_file else args.query
    gql_vars = json.loads(args.vars) if args.vars else None

    requested_reports: List[str] = []
    for report in [item.strip().lower() for item in args.report.split(",") if item.strip()]:
        if report == "all":
            requested_reports.extend(CORE_REPORTS)
        elif report == "all_extended":
            requested_reports.extend(EXTENDED_REPORTS)
        else:
            requested_reports.append(report)
    requested_reports = list(dict.fromkeys(requested_reports))
    unknown_reports = sorted(set(requested_reports) - set(REPORT_CATALOG))
    if not requested_reports or unknown_reports:
        parser.error(f"Unknown or empty report selection: {', '.join(unknown_reports) if unknown_reports else args.report}")

    enterprise_only = all(
        report == "enterprise_policies" or (report == "raw" and args.raw_scope == "enterprise")
        for report in requested_reports
    )
    verify_ssl: Union[bool, str] = False if args.insecure else (args.ca_bundle if args.ca_bundle else True)
    client_opts: Dict[str, Any] = dict(
        verify_ssl=verify_ssl,
        max_retries=args.max_retries,
        timeout=args.timeout,
        rate_buffer=args.rate_buffer,
        service_pattern=args.service_pattern,
        custom_ci_files=custom_ci,
        enterprise=args.enterprise,
        backoff_base=args.backoff_base,
        backoff_max=args.backoff_max,
    )

    clients: List[VCSClient] = []
    checked_rate_limits: Set[str] = set()
    try:
        if args.target == "combined":
            if not args.config:
                parser.error("--config is required when target is 'combined'")
            config_entries = json.loads(args.config.read_text(encoding="utf-8"))
            if not isinstance(config_entries, list) or not config_entries:
                parser.error("Combined config must be a non-empty JSON array")
            for entry in config_entries:
                target = entry.get("target")
                if target not in {"github", "ghes", "bitbucket", "bitbucket_cloud", "bitbucket_server"}:
                    parser.error(f"Unsupported target in combined config: {target}")
                auth_type = entry.get("auth_type", args.auth_type)
                entry_opts = {
                    **client_opts,
                    "enterprise": entry.get("enterprise", args.enterprise),
                    "app_id": entry.get("app_id", args.app_id),
                    "installation_id": entry.get("installation_id", args.installation_id),
                    "private_key": entry.get("private_key", args.private_key),
                }
                entry_url = entry.get("url", args.url)
                entry_type = entry.get("type", args.deployment_type)
                entry_token = entry.get("token", args.token)
                if entry.get("all_orgs"):
                    seed = build_client(target, entry_type, entry_url, entry_token, "", auth_type=auth_type, **entry_opts)
                    _preflight_client(seed, checked_rate_limits, args.rate_check, False)
                    orgs = [org for org in seed.list_orgs() if org.lower() not in excluded]
                    seed.close()
                else:
                    orgs = [org.strip() for org in str(entry.get("org", "")).split(",") if org.strip() and org.strip().lower() not in excluded]
                if not orgs and enterprise_only:
                    orgs = [entry.get("enterprise") or "site"]
                if not orgs and not enterprise_only:
                    logger.warning("No organizations/projects selected for combined target %s", target)
                for org in orgs:
                    clients.append(build_client(target, entry_type, entry_url, entry_token, org, auth_type=auth_type, **entry_opts))
        else:
            if args.all_orgs:
                seed = build_client(
                    args.target,
                    args.deployment_type,
                    args.url,
                    args.token,
                    "",
                    auth_type=args.auth_type,
                    app_id=args.app_id,
                    installation_id=args.installation_id,
                    private_key=args.private_key,
                    **client_opts,
                )
                _preflight_client(seed, checked_rate_limits, args.rate_check, False)
                orgs = [org for org in seed.list_orgs() if org.lower() not in excluded]
                logger.info("Discovered %s organizations/workspaces/projects", len(orgs))
                seed.close()
            elif enterprise_only:
                orgs = [args.org or args.enterprise or "site"]
            else:
                orgs = [org.strip() for org in args.org.split(",") if org.strip() and org.strip().lower() not in excluded]
            if not orgs:
                parser.error("Provide --org <name>, --enterprise <slug>, or use --all-orgs")
            for org in orgs:
                clients.append(
                    build_client(
                        args.target,
                        args.deployment_type,
                        args.url,
                        args.token,
                        org,
                        auth_type=args.auth_type,
                        app_id=args.app_id,
                        installation_id=args.installation_id,
                        private_key=args.private_key,
                        **client_opts,
                    )
                )

        for client in clients:
            _preflight_client(client, checked_rate_limits, args.rate_check, args.preflight)

        enrichment = load_enrichment_csv(args.enrich_csv, args.enrich_key) if args.enrich_csv else {}
        only_items = _load_retry_items(args.retry_errors)
        state_db_path = args.db_path or (args.out_dir / ".datascrape_checkpoints.db" if args.resume else None)
        db = DBManager(state_db_path) if state_db_path else None
        started_at = datetime.now(timezone.utc).isoformat()
        timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        manifest_path = args.run_dir / f"datascrape_{timestamp}_{run_id[-7:-1]}.json"
        report_cache: Dict[str, List[Dict[str, Any]]] = {}
        outputs: List[str] = []
        report_summary: Dict[str, Any] = {}
        status = "failed"
        run_error: Optional[str] = None

        logger.info(
            "Run configuration: target=%s reports=%s batch=%s workers=%s retries=%s backoff=%ss..%ss rate_check=%s classification=%s",
            args.target,
            ",".join(requested_reports),
            args.batch_size,
            args.workers,
            args.max_retries,
            args.backoff_base,
            args.backoff_max,
            args.rate_check,
            args.classification,
        )
        logger.info("Selected %s client scopes; output directory=%s", len(clients), args.out_dir)

        try:
            for report_type in requested_reports:
                rows, errors, key_fields = run_report(
                    clients=clients,
                    report_type=report_type,
                    repo_cache=report_cache,
                    workers=args.workers,
                    chunk_size=args.batch_size,
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
                    redact_sensitive=args.redact_sensitive,
                )
                if args.dry_run:
                    report_summary[report_type] = {"rows": 0, "errors": len(errors), "dry_run": True}
                    continue

                if enrichment:
                    rows = enrich_rows(rows, enrichment, ["full_name", "repo", "name", "identifier"])
                if args.redact_sensitive:
                    rows = redact_sensitive_rows(rows)
                    errors = redact_sensitive_rows(errors)  # type: ignore[assignment]

                stem = f"{args.target}_{report_type}"
                export_report(rows, args.out_dir, stem, formats, columns=columns)
                for file_format in formats:
                    candidate = args.out_dir / f"{stem}.{file_format}"
                    if candidate.exists():
                        outputs.append(str(candidate))

                rollup = build_summary_rollup(report_type, rows)
                if rollup:
                    if args.redact_sensitive:
                        rollup = redact_sensitive_rows(rollup)
                    rollup_stem = f"{stem}_summary"
                    export_report(rollup, args.out_dir, rollup_stem, formats)
                    for file_format in formats:
                        candidate = args.out_dir / f"{rollup_stem}.{file_format}"
                        if candidate.exists():
                            outputs.append(str(candidate))

                if errors:
                    error_stem = f"{stem}_errors"
                    export_report(errors, args.out_dir, error_stem, ["csv", "json"])
                    outputs.extend(str(args.out_dir / f"{error_stem}.{fmt}") for fmt in ("csv", "json"))

                upserted = 0
                if db:
                    upserted = db.upsert_rows(report_type, rows, key_fields, run_id=run_id)
                report_summary[report_type] = {"rows": len(rows), "errors": len(errors), "database_rows_upserted": upserted}
                logger.info(
                    "Report %s completed: rows=%s errors=%s database_rows_upserted=%s",
                    report_type,
                    len(rows),
                    len(errors),
                    upserted,
                )

            if not args.dry_run:
                dashboard_path = export_run_dashboard(args.out_dir, args.target, run_id, report_summary, outputs)
                outputs.append(str(dashboard_path))
            if db:
                if args.db_retention_days:
                    db.prune_history(args.db_retention_days)
                integrity = db.integrity_check()
                logger.info("SQLite integrity check: %s", integrity)
                if args.db_backup:
                    backup_path = args.db_backup_dir / f"datascrape_{run_id}.db"
                    db.backup(backup_path)
                    outputs.append(str(backup_path))
            status = "success"
        except Exception as exc:
            run_error = f"{type(exc).__name__}: {exc}"
            logger.exception("Datascrape run failed")
            raise
        finally:
            if db:
                db.close()
            finished_at = datetime.now(timezone.utc).isoformat()
            outputs.append(str(manifest_path))
            http_metrics = [
                {
                    "platform": client.platform,
                    "scope": client.org_or_project,
                    "state": client.http.get_rate_limit_state(),
                }
                for client in clients
                if getattr(client, "http", None) is not None and hasattr(client.http, "get_rate_limit_state")
            ]
            write_run_manifest(
                manifest_path,
                run_id=run_id,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
                arguments={**vars(args), "token": "[REDACTED]", "private_key": "[REDACTED]" if args.private_key else None},
                reports=requested_reports,
                outputs=outputs,
                summary={
                    "reports": report_summary,
                    "total_rows": sum(item.get("rows", 0) for item in report_summary.values()),
                    "total_errors": sum(item.get("errors", 0) for item in report_summary.values()),
                    "http_metrics": http_metrics,
                },
                log_file=active_log,
                error=run_error,
            )
            logger.info("Run manifest written: %s (status=%s)", manifest_path, status)

        removed_manifests = prune_files(args.run_dir, "datascrape_*.json", args.run_retention_days)
        if removed_manifests:
            logger.info("Removed %s expired run manifests", removed_manifests)
        if args.report_retention_days:
            report_patterns = [
                f"{prefix}_*.{extension}"
                for prefix in ("github", "ghes", "bitbucket", "combined", "diff", "trends")
                for extension in ("csv", "json", "xlsx", "html")
            ] + ["datascrape_dashboard_*.html"]
            removed_reports = sum(
                prune_files(args.out_dir, pattern, args.report_retention_days)
                for pattern in report_patterns
            )
            if removed_reports:
                logger.info("Removed %s expired report export files", removed_reports)
        logger.info("Datascrape finished successfully; rows=%s errors=%s", sum(item.get("rows", 0) for item in report_summary.values()), sum(item.get("errors", 0) for item in report_summary.values()))
    except Exception:
        raise
    finally:
        for client in clients:
            try:
                client.close()
            except Exception:
                logger.warning("Failed to close client resources cleanly", exc_info=True)


if __name__ == "__main__":
    main()
