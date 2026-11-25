# Usage Guide

## Scope selection

### One organization

```bash
datascrape github --org platform-team --report repo_audit --output csv,json
```

### Multiple organizations

```bash
datascrape github --org org-a,org-b,org-c --report repo_audit --output csv
```

### All discoverable scopes

```bash
datascrape github --all-orgs --report repo_audit --output csv,json
```

### Exclude scopes

```bash
datascrape github --all-orgs --exclude-orgs sandbox,test-org --report repo_audit
```

### Dry run

```bash
datascrape github --all-orgs --report repo_audit --dry-run
```

## Filtering repositories

`--limit` processes the first N repositories in each discovered scope. `--repo-filter` accepts a regular expression matching the repository name or full name.

```bash
datascrape github --org my-org --report ownership_baseline \
  --skip-archived --repo-filter '^(platform|payments)-' --limit 100
```

## Concurrency and chunking

```bash
datascrape github --org my-org --report all_extended \
  --batch 100 --workers 12 --max-retries 8 \
  --backoff-base 2 --backoff-max 900
```

- Increase `--batch` for fewer checkpoint boundaries.
- Increase `--workers` for more API concurrency.
- Lower both if API secondary limits recur.
- Rate-limit headers cause shared worker coordination and do not require manual retry loops.

## Report selection

Core reports:

```bash
datascrape github --org my-org --report all --output csv,json
```

Core plus selected extended reports:

```bash
datascrape github --org my-org \
  --report repo_audit,security_alerts,dora_metrics \
  --output csv,json,xlsx
```

Every built-in report:

```bash
datascrape github --all-orgs --report all_extended --output csv,json
```

Run `datascrape list-reports` for the current catalog.

## Backfills

Reports that support backfills include pull request metrics, PR reviews, issue SLA, DORA metrics, and audit-log export.

```bash
datascrape ghes --url https://github.corp.com --org my-org \
  --report pull_request_metrics \
  --backfill-since 2026-01-01T00:00:00Z \
  --backfill-until 2026-06-30T23:59:59Z \
  --db-path ./observability.db
```

## Enriching output

Create a CSV whose join column matches external repository metadata:

```csv
repo_name,product_owner,business_area,tier
org-a/platform-api,Core Platform,Platform,Tier 1
```

Join it:

```bash
datascrape github --org org-a --report repo_audit \
  --enrich-csv ./ims_repos.csv --enrich-key repo_name \
  --output csv,json
```

Each external column is exported with an `ext_` prefix.

## Export column selection

```bash
datascrape github --org my-org --report repo_audit \
  --columns full_name,org,archived,size_mb,health_status \
  --output csv,xlsx
```

## Resume and retry

Start a resumable run:

```bash
datascrape github --all-orgs --report repo_audit \
  --db-path ./assessment.db --resume --output csv,json
```

If a run is interrupted or some repositories fail, rerun:

```bash
datascrape github --all-orgs --report repo_audit \
  --db-path ./assessment.db --resume --output csv,json
```

Retry only repositories in an exported errors file:

```bash
datascrape github --all-orgs --report repo_audit \
  --retry-errors ./reports/github_repo_audit_errors.csv \
  --output csv,json
```

The retry loader reads the `item` column in the error CSV.

## SQLite history

List snapshots:

```bash
datascrape diff --db-path ./assessment.db --report repo_audit --list-runs
```

Diff the latest two snapshots:

```bash
datascrape diff --db-path ./assessment.db --report repo_audit --output csv,json
```

Diff explicit snapshots:

```bash
datascrape diff --db-path ./assessment.db --report repo_audit \
  --from-run 20261125T120000123456Z \
  --to-run 20261125T180000123456Z \
  --output csv,json
```

Export historical row counts:

```bash
datascrape trends --db-path ./assessment.db --report repo_audit --output csv,json
```

## Data retention

```bash
datascrape github --all-orgs --report all \
  --db-path ./assessment.db \
  --db-retention-days 90 \
  --log-retention-days 30 \
  --run-retention-days 90 \
  --report-retention-days 30
```

Retention is applied after successful database integrity checks and run cleanup, as applicable.

## Combined targets

Create `targets.json` and run:

```bash
datascrape combined --config ./targets.json \
  --report repo_audit,archived_inactive \
  --db-path ./combined.db --output csv,json
```

See [Configuration Reference](configuration.md) for the complete combined-target format.
