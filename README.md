# datascrape

A multi-VCS, concurrent Python CLI framework for repository audits, enterprise policy assessments, security posture checks, and engineering observability across **GitHub Cloud**, **GitHub Enterprise Server (GHES)**, **Bitbucket Cloud**, and **Bitbucket Server / Data Center**.

## Documentation

Complete usage and operational documentation is available in [`Docs/`](Docs/README.md):

- [Getting Started](Docs/getting-started.md)
- [Usage Guide](Docs/usage.md)
- [Authentication](Docs/authentication.md)
- [Report Catalog](Docs/reports.md)
- [Architecture](Docs/architecture.md)
- [Docker and Compose](Docs/docker.md)
- [Outputs and Storage](Docs/outputs-and-storage.md)
- [Configuration Reference](Docs/configuration.md)
- [Troubleshooting](Docs/troubleshooting.md)
- [Development](Docs/development.md)

---

## Features

- **Multi-Platform Client Architecture:** Run single-platform scans (`github`, `ghes`, `bitbucket`) or aggregate multiple servers and platforms into one unified report (`combined`).
- **28 Built-In Reports + Universal Escape Hatches:** Covers ownership baselines, repo audits, configuration gaps, app/service-account inventories, enterprise policies, security alerts, CI/CD, DORA/PR metrics, and billing—plus `--report raw` (any REST endpoint) and `--report graphql` (any GraphQL query).
- **Chunked Concurrency & Checkpointing:** Configurable thread pools (`--workers`) and batch sizes (`--chunk-size`) with a live terminal progress bar, per-repo error isolation, `--resume` checkpoints, and `--retry-errors` reprocessing.
- **Flexible Outputs & Observability:** Exports **CSV**, **JSON**, **Excel**, and **HTML**, generates executive rollups, supports enrichment, run manifests, and SQLite history/diffs.
- **Resilient Rate Handling:** Shared, thread-safe rate-limit coordination; `/rate_limit` preflight; `Retry-After` and reset-header handling; exponential backoff with jitter; and retry support for transient network/5xx errors.
- **Operational Logging:** Standard-library logging to console and a timestamped `logs/datascrape_YYYYMMDD_HHMMSS_microseconds.log` file, with secret redaction and JSON run manifests.
- **Enterprise Authentication & Security:** GitHub App installation tokens, PAT support, Bitbucket bearer/basic auth, corporate CA bundles, non-root Docker execution, and optional sensitive-field redaction.

---

## Installation

Open a terminal in the top-level project directory (containing `pyproject.toml`) and install in editable mode:

```bash
python -m pip install -e ".[dev]"
```

For a pinned runtime dependency install (the same dependency set used by the Docker image):

```bash
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
```

Verify the installation and view all available reports:

```bash
datascrape --help
datascrape list-reports
```

> **Windows PATH Note:** If `datascrape` is not recognized after installing, add your Python `Scripts` directory to your User `PATH` environment variable or invoke the CLI via `python -m datascrape.cli <args>`.

---

## Authentication & Required Permissions

Pass tokens securely via environment variables using the `ENV:` prefix so credentials stay out of shell history and config files:

```cmd
:: Windows Command Prompt
set GH_TOKEN=ghp_your_token_here
```

```bash
# PowerShell
$env:GH_TOKEN="ghp_your_token_here"

# macOS / Linux
export GH_TOKEN="ghp_your_token_here"
```

### Recommended Token Scopes

| Platform | Auth Type | Recommended Permissions / Notes |
| :--- | :--- | :--- |
| **GitHub Cloud** | `bearer` (default) | Fine-grained or classic PAT with the required repository/org permissions. Add audit-log and enterprise permissions for the reports that need them. **Must authorize SAML SSO** for each org if SSO is enabled. |
| **GitHub App** | `github_app` | App ID, installation ID, and private key PEM. Installation access tokens are generated and refreshed automatically. Use `--auth-type github_app --app-id ... --installation-id ... --private-key ...`. |
| **GHES (Self-Hosted)** | `bearer` | Same scopes as above. Site-admin tokens automatically discover all orgs across the instance with `--all-orgs`. |
| **Bitbucket Cloud** | `bearer` or `basic` | Workspace/Repo Access Token (`--auth-type bearer`) or legacy App Password (`--auth-type basic --token ENV:BB_CREDS` formatted as `username:app_password`). |
| **Bitbucket Server / DC** | `bearer` | HTTP Access Token with Project/Repository Read (and Admin read for permissions). |

*Note: If your token lacks admin rights on specific repos or enterprise settings, `datascrape` will not crash—it records `ACCESS_DENIED` in those columns and completes the run.*

---

## Docker

The repository includes a non-root Docker image and a hardened Compose service. No inbound ports are required; Datascrape makes outbound API requests only.

```bash
cp .env.example .env
# Set GH_TOKEN or GHES_TOKEN in .env, then:
docker compose build
docker compose run --rm datascrape list-reports
docker compose run --rm datascrape github --org my-org --report repo_audit --batch 50 --workers 8 --token ENV:GH_TOKEN --output csv,json,xlsx,html --out-dir /data/reports --log-dir /data/logs
```

Compose mounts `/data`, `reports`, `logs`, `runs`, and `backups` for persistent artifacts, drops Linux capabilities, enables `no-new-privileges`, and uses a read-only container root filesystem with a writable `/tmp` tmpfs. The Compose user defaults to UID/GID 1000; set `DATASCRAPE_UID` and `DATASCRAPE_GID` in `.env` to match your host user on Linux so bind-mounted output directories remain writable. Mount a corporate CA bundle into the container and pass `--ca-bundle` when scanning internal GHES/Bitbucket installations.

## Rate Limits, Batches, Threads, and Logs

GitHub runs check `/rate_limit` before scanning by default. Every response is inspected for `X-RateLimit-Remaining`, `X-RateLimit-Reset`, and `X-RateLimit-Resource`. Primary quota exhaustion waits for the server reset timestamp (which may be close to an hour); secondary limits honor `Retry-After` or wait at least 60 seconds with increasing exponential backoff. A shared coordinator pauses all workers using the same API host and credential. Network errors and transient HTTP 408/425/429/5xx responses also retry with exponential backoff and jitter. Server reset/`Retry-After` values take precedence over the configured backoff cap.

`--batch` controls repositories per batch; `--workers` and `--threads` are aliases for the same bounded thread-pool size. `--chunk-size` remains as a backward-compatible alias for `--batch`.

```bash
datascrape github --org my-org --report all_extended \
  --batch 100 --workers 12 --max-retries 8 \
  --backoff-base 2 --backoff-max 900 --rate-check \
  --rate-buffer 10 --resume --log-level INFO
```

Each invocation writes a unique `logs/datascrape_<timestamp>.log` file and `runs/datascrape_*.json` manifest. Use `--log-level DEBUG`, `--log-dir`, `--log-file`, or `--no-file-log` to control logging. Secrets are redacted from log records. `--redact-sensitive` also redacts common token, password, private-key, and webhook URL fields from exported, checkpointed, and persisted report rows. `--classification` records the declared sensitivity of each run in its manifest.

## GitHub App Authentication

GitHub App installation authentication is supported for GitHub Cloud and GHES. The CLI creates a short-lived RS256 App JWT, exchanges it for an installation token, caches the token until shortly before expiry, and refreshes it automatically. Token-endpoint network errors, rate limits, and transient 5xx responses are retried. Keep the PEM outside the repository; mount it read-only or provide it through a protected environment variable.

```bash
datascrape github \
  --auth-type github_app \
  --app-id ENV:GH_APP_ID \
  --installation-id ENV:GH_INSTALLATION_ID \
  --private-key /certs/github-app.pem \
  --org my-org \
  --report all \
  --batch 50 --threads 8
```

For a combined run, use `targets.github-app.example.json`. The supplied Compose service mounts `./certs` read-only at `/certs`.

## Quick Start Examples

### 1. Fast Smoke Test (First 5 Repos Per Org)
Validate credentials, org discovery, and permissions before a full scan:

```bash
datascrape github --type cloud --url https://api.github.com --token ENV:GH_TOKEN \
  --all-orgs --report repo_audit,repo_configurations --limit 5 --output csv,json
```

### 2. Core Assessment Across All Orgs (Resumable + IMS Enrichment + DB)
Runs `ownership_baseline`, `apps_service_accounts`, `archived_inactive`, `repo_audit`, and `repo_configurations` in one pass with cached repo discovery:

```bash
datascrape github --type cloud --url https://api.github.com --token ENV:GH_TOKEN \
  --all-orgs --report all \
  --enrich-csv ims_repos.csv --enrich-key repo_name \
  --db-path ./assessment.db --resume \
  --workers 6 --chunk-size 50 --output csv,json
```

### 3. Enterprise-Layer Policies & App Owners
Pulls enterprise governance settings (GraphQL `EnterpriseOwnerInfo` + REST Actions, rulesets, security configs, Copilot, licensing) plus installed apps and owners:

```bash
datascrape github --type cloud --url https://api.github.com --token ENV:GH_TOKEN \
  --enterprise YOUR_ENTERPRISE_SLUG --all-orgs \
  --report enterprise_policies,apps_service_accounts --output csv,json
```

### 4. Target Specific Orgs or Exclude Sandboxes

```bash
# Single org
datascrape ghes --type self_hosted --url https://github.corp.com --token ENV:GHES_TOKEN \
  --org core-platform --report repo_audit --output csv

# Multiple specific orgs
datascrape github --type cloud --url https://api.github.com --token ENV:GH_TOKEN \
  --org org-a,org-b,org-c --report repo_audit --output csv,json

# All orgs except specific ones
datascrape github --type cloud --url https://api.github.com --token ENV:GH_TOKEN \
  --all-orgs --exclude-orgs sandbox-org,test-org --report repo_audit --output csv,json
```

### 5. Backfill Pull Request Metrics into SQLite

```bash
datascrape ghes --type self_hosted --url https://github.corp.com --token ENV:GHES_TOKEN \
  --org my-org --report pull_request_metrics \
  --backfill-since 2026-01-01T00:00:00Z --backfill-until 2026-06-30T23:59:59Z \
  --db-path ./observability.db --output json
```

### 6. Combined Multi-Client Report
Create a `targets.json` file (referencing environment variables instead of raw tokens):

```json
[
  {
    "target": "github",
    "type": "cloud",
    "url": "https://api.github.com",
    "token": "ENV:GH_TOKEN",
    "enterprise": "my-corp",
    "all_orgs": true
  },
  {
    "target": "bitbucket",
    "type": "self_hosted",
    "url": "https://bitbucket.corp.com",
    "token": "ENV:BB_TOKEN",
    "org": "PROJ1,PROJ2"
  }
]
```

Run a combined report across every configured target:

```bash
datascrape combined --config targets.json \
  --report repo_audit,archived_inactive \
  --db-path ./observability.db --output csv,json
```

### 7. Universal Escape Hatches (`raw` & `graphql`)

```bash
# Org-scoped REST endpoint
datascrape github --type cloud --url https://api.github.com --token ENV:GH_TOKEN \
  --org my-org --report raw --raw-scope org --endpoint /orgs/{org}/actions/runners --output csv,json

# Repo-scoped REST endpoint (chunked & concurrent across every repo)
datascrape github --type cloud --url https://api.github.com --token ENV:GH_TOKEN \
  --all-orgs --report raw --raw-scope repo --endpoint /repos/{full_name}/autolinks --output csv,json

# GraphQL query (e.g. ProjectsV2, IP allow lists, custom enterprise queries)
datascrape github --type cloud --url https://api.github.com --token ENV:GH_TOKEN \
  --org my-org --report graphql --query-file query.graphql --output json
```

### 8. Re-run Failed Repos & Diff Historical Snapshots

```bash
# Re-scrape only repos listed in an error file
datascrape github --type cloud --url https://api.github.com --token ENV:GH_TOKEN \
  --all-orgs --report repo_audit --retry-errors ./reports/github_repo_audit_errors.csv --output csv,json

# List recorded snapshots in SQLite
datascrape diff --db-path ./assessment.db --report repo_audit --list-runs

# Diff the two most recent runs (or pass --from-run / --to-run)
datascrape diff --db-path ./assessment.db --report repo_audit --output csv,json
```

---

## Report Catalog

Run `datascrape list-reports` at any time to print this catalog in your terminal. Pass a single report, a comma-separated list, `--report all` (5 core reports), or `--report all_extended` (all 28 built-in reports).

### Core Assessment & Enterprise Governance

| Report Name | Scope | What It Captures |
| :--- | :--- | :--- |
| `ownership_baseline` | Repo | Active repos with `CODEOWNERS`, custom property owners, and multi-CI detection (GitHub Actions, Jenkins, Azure Pipelines, CircleCI, GitLab CI, Bitbucket Pipelines, Travis). Auto-rollup by business area/org. |
| `apps_service_accounts` | Org | Installed GitHub Apps (registered owner, owner type, permissions, events, audit-log installer, org-admin fallback) and candidate bot/service accounts. |
| `archived_inactive` | Repo | Archived vs. stale repos (configurable `--inactive-days`), disk size (KB/MB), and proposed disposition. Auto-rollup by org/status/disposition. |
| `repo_audit` | Repo | Size, LFS detection (`.gitattributes`/`.lfsconfig`), Actions permissions & workflow counts, repo secret counts/names, classic branch protections, rulesets, teams, direct assignments, outside collaborators, and admins. Auto-rollup by org. |
| `repo_configurations` | Repo | Full settings matrix (visibility, merge options, GHAS, secret scanning, push protection, Dependabot alerts/updates, default branch protection, webhooks/deploy keys, governance files) plus `configs_present` and `configs_missing` lists. |
| `enterprise_policies` | Enterprise | Enterprise-layer governance policies (member/repo creation, 2FA, SAML SSO, IP allow lists), Actions policies, runner groups, enterprise rulesets, code security configs, custom property schemas, Copilot policies, licensing, and GHES server metadata. |

### Extended Inventory, IAM & Security

| Report Name | Scope | What It Captures |
| :--- | :--- | :--- |
| `languages` | Repo | Language percentage breakdown and byte totals per repo. |
| `releases_tags` | Repo | Release counts, latest tag/date, sampled tags, and attached binary asset sizes. |
| `submodules` | Repo | Submodule paths and target URLs parsed from `.gitmodules`. |
| `teams_members` | Org | Team slugs, privacy, parent hierarchy, permissions, and member lists. |
| `webhooks_deploy_keys` | Repo | Full webhook URLs, active/SSL flags, events, and deploy key details. |
| `iam_tokens_sso` | Org | Approved fine-grained PATs and SAML SSO credential authorizations. |
| `dormant_users` | Org | Org members cross-referenced against recent org events. |
| `security_alerts` | Org | Open Code Scanning, Secret Scanning, and Dependabot alerts with severity rollup. |
| `org_secrets_variables` | Org | Org-level Actions secrets (names/visibility) and variables. |
| `sbom_licenses` | Repo | Repository SPDX licenses and dependency-graph SBOM package/license counts. |
| `actions_pinning` | Repo | Supply-chain audit of `.github/workflows/*.yml` `uses:` references not pinned to 40-char commit SHAs. |
| `audit_log` | Org | Organization audit-log export (supports `--backfill-since` / `--backfill-until`). |

### CI/CD, Engineering Metrics & Billing

| Report Name | Scope | What It Captures |
| :--- | :--- | :--- |
| `runners_packages` | Org | Self-hosted runners, Actions cache usage, and org packages (container, npm, maven, nuget, rubygems). |
| `environments` | Repo | Deployment environments, protection rules, and environment secret names. |
| `workflow_runs` | Repo | Recent workflow pass/fail rates, average duration, and p95 duration. |
| `pages` | Repo | GitHub Pages status, URLs, custom domains, and HTTPS enforcement. |
| `pull_request_metrics` | Repo | PR inventory, draft/merge state, timestamps, and merge cycle time hours (supports date backfill). |
| `pr_reviews` | Repo | Time-to-first-review hours, approval counts, and reviewer lists. |
| `contributors_bus_factor` | Repo | Top contributor commit share and bus-factor risk rating (`LOW`/`MEDIUM`/`HIGH`). |
| `issues_sla` | Repo | Issue backlog, labels, and time-to-close SLA hours (excludes PRs). |
| `dora_metrics` | Repo | Deployments, releases, merged PR counts, and average/p95 PR lead times. |
| `billing_copilot` | Org | Actions/Packages/Storage billing, GHAS committers, Copilot seats, and Codespaces. |

### Universal Escape Hatches

| Report Name | Scope | What It Captures |
| :--- | :--- | :--- |
| `raw` | `org` / `repo` / `enterprise` | Queries any REST endpoint via `--endpoint` (supports `{org}`, `{enterprise}`, `{repo}`, `{full_name}` placeholders, `--param key=val`, and `--extract-key`). |
| `graphql` | Org | Executes any GraphQL query via `--query` or `--query-file` with optional `--vars` JSON. |

---

## CLI Options Reference

| Category | Flag | Description |
| :--- | :--- | :--- |
| **Target & Auth** | `target` | Positional: `github`, `ghes`, `bitbucket`, or `combined`. |
| | `--type` | `cloud` (default) or `self_hosted`. |
| | `--url` | Base server or API URL (default: `https://api.github.com`). |
| | `--token` | Raw token or `ENV:VAR_NAME`. |
| | `--auth-type` | `bearer`, `basic`, or `github_app`. |
| | `--app-id` / `--installation-id` / `--private-key` | GitHub App credentials; private key may be a PEM path or `ENV:VAR_NAME`. |
| | `--org` | Single org/workspace/project or comma-separated list. |
| | `--enterprise` | Enterprise slug for `enterprise_policies` or enterprise-scoped `raw` queries. |
| | `--all-orgs` | Auto-discover all accessible orgs/workspaces/projects. |
| | `--exclude-orgs` | Comma-separated orgs to skip. |
| | `--config` | Path to `targets.json` when using `combined`. |
| **Output & Join** | `--output` | Comma-delimited formats: `csv,json,xlsx,html` (default `csv,json`). |
| | `--out-dir` | Destination folder (default: `./reports`). |
| | `--columns` | Comma-separated subset/order of columns to export. |
| | `--enrich-csv` | External CSV (IMS, CMDB, app owners) to left-join onto results (`ext_*` columns). |
| | `--enrich-key` | Join column name inside `--enrich-csv` (matches `full_name`, `repo`, `name`, or `identifier`). |
| **Customization** | `--service-pattern` | Custom regex for flagging service/bot accounts. |
| | `--custom-ci-files` | Comma-separated extra CI file paths to detect in `ownership_baseline`. |
| | `--inactive-days` | Inactivity cutoff in days (default: `180`). |
| **Scale & Filter** | `--workers` / `--threads` | Thread pool concurrency (default: `8`). Both names set the same worker count. |
| | `--batch` / `--chunk-size` | Repositories processed per batch/checkpoint (default: `50`). |
| | `--limit` | Process only the first `N` repos per org (smoke testing). |
| | `--repo-filter` | Regex filter matched against repo `name` and `full_name`. |
| | `--skip-archived` | Exclude archived repositories. |
| | `--dry-run` | Preview discovered orgs and repo counts without executing deep API calls. |
| **DB & Recovery** | `--db-path` | SQLite database file for upserts, snapshots, and checkpoints. |
| | `--db-backup` / `--db-backup-dir` | Create a timestamped SQLite backup after a successful run. |
| | `--db-retention-days` | Prune historical snapshots and checkpoints older than the specified age. |
| | `--backfill-since` / `--backfill-until` | ISO-8601 date bounds (e.g., `2026-01-01T00:00:00Z`) for PRs, issues, audit logs, and DORA. |
| | `--resume` | Resume from last saved chunk checkpoint in SQLite. |
| | `--retry-errors` | Re-run only failed items listed in a `*_errors.csv` file. |
| **Network** | `--max-retries` / `--backoff-base` / `--backoff-max` | Tune retries (`8`), exponential retry base (`2s`), and backoff cap (`900s`). Server reset and `Retry-After` directives take precedence. |
| | `--rate-check` / `--rate-buffer` | Check GitHub quota before execution (enabled by default) and pause before the configured remaining-request threshold (`10`). |
| | `--ca-bundle` | Path to corporate root CA `.pem`/`.crt` bundle. |
| | `--insecure` | Disable SSL certificate verification (use only behind corporate SSL inspection if no CA bundle is available). |
| **Logging & Audit** | `--log-dir` / `--log-level` / `--log-file` | Configure timestamped file logs, verbosity, and optional filename override. |
| | `--no-file-log` / `--verbose` | Disable file logging or enable DEBUG output. |
| | `--run-dir` | Directory for JSON run manifests containing arguments, output paths, timestamps, and counts. |
| | `--log-retention-days` / `--run-retention-days` / `--report-retention-days` | Optional cleanup windows for generated logs, manifests, and report exports. |
| | `--redact-sensitive` / `--classification` | Redact common sensitive fields and record the declared data classification. |
| | `--preflight` | Validate GitHub quota and organization visibility before repository processing (enabled by default). |

---

## Output Files & SQLite Schema

Report exports are written to `--out-dir` (default `./reports`):

- `<target>_<report>.csv`, `.json`, `.xlsx`, and/or `.html` — Full normalized dataset in the selected formats.
- `<target>_<report>_summary.*` — Automatic leadership rollups (where defined).
- `<target>_<report>_errors.csv` and `.json` — Created only if individual items fail, enabling targeted recovery with `--retry-errors`.
- `datascrape_dashboard_<run_id>.html` — Run-level executive summary with report counts and links to generated exports.

Operational artifacts are stored separately:

- `logs/datascrape_YYYYMMDD_HHMMSS_microseconds.log` — Console-equivalent timestamped run log (unless `--no-file-log` is supplied).
- `runs/datascrape_*.json` — Run manifest with safe arguments, timestamps, outputs, data classification, HTTP metrics, and row/error counts.

When `--db-path` is provided, `datascrape` maintains three SQLite tables:

1. `report_records` — Latest upserted state keyed by `(report_type, record_key)`.
2. `report_snapshots` — Immutable point-in-time records keyed by `(run_id, report_type, record_key)` used by `datascrape diff`.
3. `checkpoints` — Per-batch completion state used by `--resume`.

Use `--db-backup` to create a timestamped backup and `--db-retention-days` to prune old snapshots/checkpoints. Use `datascrape diff --db-path ./assessment.db --report repo_audit` to compare snapshots and `datascrape trends --db-path ./assessment.db --report repo_audit` to export historical snapshot counts. SQLite is configured with WAL journaling, a busy timeout, and a schema version marker. SIGINT/SIGTERM requests stop workers at the current batch boundary and persist successful completions before exiting; use `--resume` to continue.

---

## Project Structure

```text
datascrape/
├── pyproject.toml
├── README.md
├── Dockerfile
├── compose.yaml
├── .pre-commit-config.yaml
├── .github/workflows/
└── datascrape/
    ├── __init__.py
    ├── cli.py               # CLI entrypoint, configuration, preflight, run lifecycle
    ├── auth.py              # GitHub App installation-token provider
    ├── http_client.py       # Thread-local sessions, shared rate coordination and retries
    ├── logging_utils.py     # Timestamped console/file logging and secret redaction
    ├── run_metadata.py      # JSON run manifests
    ├── shutdown.py          # Cooperative SIGINT/SIGTERM handling
    ├── progress.py          # Terminal progress and structured progress logs
    ├── reports.py           # Report catalog, bounded batches, concurrency, checkpoints
    ├── storage.py           # CSV/JSON/XLSX/HTML, enrichment, SQLite history/backup
    └── clients/
        ├── __init__.py      # Client factory & ENV: secret resolver
        ├── base.py          # Unified VCSClient abstract interface
        ├── github.py        # GitHub Cloud & GHES implementation
        └── bitbucket.py     # Bitbucket Cloud & Server/DC implementation
```

---

## Licensing

No license has been selected in this modernization. The repository owner should add the appropriate `LICENSE` file before distributing or reusing the project under open-source terms.

---

## Troubleshooting

- **`SSLError` / certificate verify failed:** Pass `--ca-bundle C:\path\to\corp-root-ca.pem` (preferred) or `--insecure` on corporate networks with TLS inspection.
- **`ACCESS_DENIED` in specific columns:** Your token can read the repository but lacks admin/owner permissions for endpoints like `/actions/secrets`, `/collaborators`, `/installations`, or enterprise settings. Check token scopes and verify SAML SSO authorization.
- **Primary or secondary rate limits:** Datascrape reads GitHub's rate-limit headers and `/rate_limit` endpoint, coordinates pauses across workers, honors `Retry-After` and `X-RateLimit-Reset`, and retries automatically. Reduce `--workers` or `--batch` if secondary limits recur frequently; keep `--resume` enabled for long scans.
- **Empty results on GitHub Cloud orgs:** Confirm the PAT has been explicitly SSO-authorized under **Settings → Developer settings → Personal access tokens → Configure SSO**.
- **403 / ACCESS_DENIED:** Verify organization membership and the token or GitHub App installation permissions required by the selected report. The organization preflight checks visibility, but individual reports can require additional permissions.
- **GitHub App token errors:** Confirm the App ID, installation ID, private key, installation target, and API host are correct. Never commit the PEM file; provide it through a protected mount or `ENV:` variable.
- **Docker reports/logs disappear:** Mount persistent host directories and use `/data/reports`, `/data/logs`, and `/data/datascrape.db` inside the container.
