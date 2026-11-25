# datascrape

A multi-VCS, concurrent Python CLI framework for repository audits, enterprise policy assessments, security posture checks, and engineering observability across **GitHub Cloud**, **GitHub Enterprise Server (GHES)**, **Bitbucket Cloud**, and **Bitbucket Server / Data Center**.

---

## Features

- **Multi-Platform Client Architecture:** Run single-platform scans (`github`, `ghes`, `bitbucket`) or aggregate multiple servers and platforms into one unified report (`combined`).
- **28 Built-In Reports + Universal Escape Hatches:** Covers ownership baselines, repo audits, configuration gaps, app/service-account inventories, enterprise policies, security alerts, CI/CD, DORA/PR metrics, and billing—plus `--report raw` (any REST endpoint) and `--report graphql` (any GraphQL query).
- **Resilient Rate-Limit & Retry Engine:** Thread-safe global pause when primary or secondary rate limits trigger, automatic `Retry-After` / `X-RateLimit-Reset` handling, and exponential backoff with jitter for transient 5xx and network errors.
- **Chunked Concurrency & Checkpointing:** Configurable thread pools (`--workers`) and batch sizes (`--chunk-size`) with a live terminal progress bar, per-repo error isolation, `--resume` checkpoints, and `--retry-errors` reprocessing.
- **Flexible Outputs & Observability:** Exports flattened **CSV**, **JSON**, or both (`--output csv,json`), auto-generates executive `*_summary` rollups, joins external CSVs (IMS/CMDB/owner lists) via `--enrich-csv`, and upserts latest state + dated snapshots into **SQLite** with built-in snapshot diffing (`datascrape diff`).
- **Zero C-Extension Dependencies:** Requires only Python 3.10+ and `requests`.

---

## Installation

Open a terminal in the top-level project directory (containing `pyproject.toml`) and install in editable mode:

```bash
python -m pip install -e .
```

Verify the installation and view all available reports:

```bash
datascrape --help
datascrape list-reports
```

> **Windows PATH Note:** If `datascrape` is not recognized after installing, either add your Python `Scripts` directory to your User `PATH` environment variable or invoke the CLI via `python -m datascrape.cli <args>`.

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
| **GitHub Cloud** | `bearer` (default) | Classic PAT with `repo`, `read:org`. Add `read:audit_log` for app installer lookup / audit logs, and `read:enterprise` or `admin:enterprise` for `enterprise_policies`. **Must authorize SAML SSO** for each org if SSO is enabled. |
| **GHES (Self-Hosted)** | `bearer` | Same scopes as above. Site-admin tokens automatically discover all orgs across the instance with `--all-orgs`. |
| **Bitbucket Cloud** | `bearer` or `basic` | Workspace/Repo Access Token (`--auth-type bearer`) or legacy App Password (`--auth-type basic --token ENV:BB_CREDS` formatted as `username:app_password`). |
| **Bitbucket Server / DC** | `bearer` | HTTP Access Token with Project/Repository Read (and Admin read for permissions). |

*Note: If your token lacks admin rights on specific repos or enterprise settings, `datascrape` will not crash—it records `ACCESS_DENIED` in those columns and completes the run.*

---

## Quick Start Examples

### 1. Fast Smoke Test (First 5 Repos Per Org)
Validate credentials, org discovery, and permissions before a full scan:

```bash
datascrape github --type cloud --url [https://api.github.com](https://api.github.com) --token ENV:GH_TOKEN \
  --all-orgs --report repo_audit,repo_configurations --limit 5 --output csv,json
```

### 2. Core Assessment Across All Orgs (Resumable + IMS Enrichment + DB)
Runs `ownership_baseline`, `apps_service_accounts`, `archived_inactive`, `repo_audit`, and `repo_configurations` in one pass with cached repo discovery:

```bash
datascrape github --type cloud --url [https://api.github.com](https://api.github.com) --token ENV:GH_TOKEN \
  --all-orgs --report all \
  --enrich-csv ims_repos.csv --enrich-key repo_name \
  --db-path ./assessment.db --resume \
  --workers 6 --chunk-size 50 --output csv,json
```

### 3. Enterprise-Layer Policies & App Owners
Pulls enterprise governance settings (GraphQL `EnterpriseOwnerInfo` + REST Actions, rulesets, security configs, Copilot, licensing) plus installed apps and owners:

```bash
datascrape github --type cloud --url [https://api.github.com](https://api.github.com) --token ENV:GH_TOKEN \
  --enterprise YOUR_ENTERPRISE_SLUG --all-orgs \
  --report enterprise_policies,apps_service_accounts --output csv,json
```

### 4. Target Specific Orgs or Exclude Sandboxes

```bash
# Single org
datascrape ghes --type self_hosted --url [https://github.corp.com](https://github.corp.com) --token ENV:GHES_TOKEN \
  --org core-platform --report repo_audit --output csv

# Multiple specific orgs
datascrape github --type cloud --url [https://api.github.com](https://api.github.com) --token ENV:GH_TOKEN \
  --org org-a,org-b,org-c --report repo_audit --output csv,json

# All orgs except specific ones
datascrape github --type cloud --url [https://api.github.com](https://api.github.com) --token ENV:GH_TOKEN \
  --all-orgs --exclude-orgs sandbox-org,test-org --report repo_audit --output csv,json
```

### 5. Backfill Pull Request Metrics into SQLite

```bash
datascrape ghes --type self_hosted --url [https://github.corp.com](https://github.corp.com) --token ENV:GHES_TOKEN \
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
    "url": "[https://api.github.com](https://api.github.com)",
    "token": "ENV:GH_TOKEN",
    "enterprise": "my-corp",
    "all_orgs": true
  },
  {
    "target": "bitbucket",
    "type": "self_hosted",
    "url": "[https://bitbucket.corp.com](https://bitbucket.corp.com)",
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
datascrape github --type cloud --url [https://api.github.com](https://api.github.com) --token ENV:GH_TOKEN \
  --org my-org --report raw --raw-scope org --endpoint /orgs/{org}/actions/runners --output csv,json

# Repo-scoped REST endpoint (chunked & concurrent across every repo)
datascrape github --type cloud --url [https://api.github.com](https://api.github.com) --token ENV:GH_TOKEN \
  --all-orgs --report raw --raw-scope repo --endpoint /repos/{full_name}/autolinks --output csv,json

# GraphQL query (e.g. ProjectsV2, IP allow lists, custom enterprise queries)
datascrape github --type cloud --url [https://api.github.com](https://api.github.com) --token ENV:GH_TOKEN \
  --org my-org --report graphql --query-file query.graphql --output json
```

### 8. Re-run Failed Repos & Diff Historical Snapshots

```bash
# Re-scrape only repos listed in an error file
datascrape github --type cloud --url [https://api.github.com](https://api.github.com) --token ENV:GH_TOKEN \
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
| | `--auth-type` | `bearer` (default) or `basic` (Bitbucket `user:app_password`). |
| | `--org` | Single org/workspace/project or comma-separated list. |
| | `--enterprise` | Enterprise slug for `enterprise_policies` or enterprise-scoped `raw` queries. |
| | `--all-orgs` | Auto-discover all accessible orgs/workspaces/projects. |
| | `--exclude-orgs` | Comma-separated orgs to skip. |
| | `--config` | Path to `targets.json` when using `combined`. |
| **Output & Join** | `--output` | Comma-delimited formats: `csv,json` (default), `csv`, or `json`. |
| | `--out-dir` | Destination folder (default: `./reports`). |
| | `--columns` | Comma-separated subset/order of columns to export. |
| | `--enrich-csv` | External CSV (IMS, CMDB, app owners) to left-join onto results (`ext_*` columns). |
| | `--enrich-key` | Join column name inside `--enrich-csv` (matches `full_name`, `repo`, `name`, or `identifier`). |
| **Customization** | `--service-pattern` | Custom regex for flagging service/bot accounts. |
| | `--custom-ci-files` | Comma-separated extra CI file paths to detect in `ownership_baseline`. |
| | `--inactive-days` | Inactivity cutoff in days (default: `180`). |
| **Scale & Filter** | `--workers` | Thread pool concurrency (default: `8`; use `4`–`6` on large GitHub runs). |
| | `--chunk-size` | Repos processed per batch/checkpoint (default: `50`). |
| | `--limit` | Process only the first `N` repos per org (smoke testing). |
| | `--repo-filter` | Regex filter matched against repo `name` and `full_name`. |
| | `--skip-archived` | Exclude archived repositories. |
| | `--dry-run` | Preview discovered orgs and repo counts without executing deep API calls. |
| **DB & Recovery** | `--db-path` | SQLite database file for upserts, snapshots, and checkpoints. |
| | `--backfill-since` / `--backfill-until` | ISO-8601 date bounds (e.g., `2026-01-01T00:00:00Z`) for PRs, issues, audit logs, and DORA. |
| | `--resume` | Resume from last saved chunk checkpoint in SQLite. |
| | `--retry-errors` | Re-run only failed items listed in a `*_errors.csv` file. |
| **Network** | `--max-retries` / `--timeout` / `--rate-buffer` | Tune retries (`6`), HTTP timeout (`30s`), and proactive rate-limit pause buffer (`10`). |
| | `--ca-bundle` | Path to corporate root CA `.pem`/`.crt` bundle. |
| | `--insecure` | Disable SSL certificate verification (use only behind corporate SSL inspection if no CA bundle is available). |

---

## Output Files & SQLite Schema

Every run writes to `--out-dir` (default `./reports`):

- `<target>_<report>.csv` & `.json` — Full normalized dataset.
- `<target>_<report>_summary.csv` & `.json` — Automatic leadership rollup (generated for `ownership_baseline`, `apps_service_accounts`, `archived_inactive`, `repo_audit`, `enterprise_policies`, `security_alerts`, `actions_pinning`, and `contributors_bus_factor`).
- `<target>_<report>_errors.csv` & `.json` — Created only if individual items fail, enabling targeted recovery with `--retry-errors`.

When `--db-path` is provided, `datascrape` maintains three SQLite tables:

1. `report_records` — Latest upserted state keyed by `(report_type, record_key)`.
2. `report_snapshots` — Immutable point-in-time records keyed by `(run_id, report_type, record_key)` used by `datascrape diff`.
3. `checkpoints` — Per-chunk completion state used by `--resume`.

---

## Project Structure

```text
datascrape1.1/
├── pyproject.toml
├── README.md
└── datascrape/
    ├── __init__.py
    ├── cli.py               # CLI entrypoint, list-reports, and diff subcommand
    ├── http_client.py       # Thread-safe requests, global rate-limit pause, exponential backoff
    ├── progress.py          # Live terminal progress bar & rate-limit telemetry
    ├── reports.py           # Report catalog, chunked concurrency, repo cache & rollups
    ├── storage.py           # Flattened CSV/JSON export, CSV enrichment, SQLite upsert/diff
    └── clients/
        ├── __init__.py      # Client factory & ENV: secret resolver
        ├── base.py          # Unified VCSClient abstract interface
        ├── github.py        # GitHub Cloud & GHES implementation
        └── bitbucket.py     # Bitbucket Cloud & Server/DC implementation
```

---

## Troubleshooting

- **`SSLError` / certificate verify failed:** Pass `--ca-bundle C:\path\to\corp-root-ca.pem` (preferred) or `--insecure` on corporate networks with TLS inspection.
- **`ACCESS_DENIED` in specific columns:** Your token can read the repository but lacks admin/owner permissions for endpoints like `/actions/secrets`, `/collaborators`, `/installations`, or enterprise settings. Check token scopes and verify SAML SSO authorization.
- **Secondary rate limits on large scans:** Lower `--workers` to `4`–`6` and keep `--resume` enabled; `datascrape` will automatically pause all threads and resume once the reset window clears.
- **Empty results on GitHub Cloud orgs:** Confirm the PAT has been explicitly SSO-authorized under **Settings → Developer settings → Personal access tokens → Configure SSO**.
