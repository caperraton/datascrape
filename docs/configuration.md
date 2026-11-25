# Configuration Reference

## CLI syntax

```text
datascrape <target> [options]
```

Targets:

- `github` — GitHub Cloud
- `ghes` — GitHub Enterprise Server
- `bitbucket` — Bitbucket Cloud or Server/Data Center
- `combined` — multiple targets from a JSON config

Utility commands:

```text
datascrape list-reports
datascrape diff --db-path DATABASE --report REPORT
datascrape trends --db-path DATABASE --report REPORT
```

## Target and authentication

| Option | Default | Description |
|---|---|---|
| `--type` | `cloud` | `cloud` or `self_hosted` |
| `--url` | GitHub API URL | Base API/server URL |
| `--token` | empty | Raw token or `ENV:VARIABLE` |
| `--auth-type` | `bearer` | `bearer`, `basic`, or `github_app` |
| `--app-id` | unset | GitHub App ID or `ENV:` reference |
| `--installation-id` | unset | Installation ID or `ENV:` reference |
| `--private-key` | unset | PEM path or `ENV:` reference |
| `--org` | unset | Comma-separated scopes |
| `--enterprise` | unset | Enterprise slug for enterprise policies |
| `--all-orgs` | false | Discover accessible org/workspace/project scopes |
| `--exclude-orgs` | empty | Comma-separated scopes to omit |
| `--config` | unset | Combined-run JSON path |

Bitbucket `--org` means workspace or project, depending on deployment type.

## Report selection

```bash
--report repo_audit
--report repo_audit,repo_configurations
--report all
--report all_extended
--report raw
--report graphql
```

- `all` selects the five core reports.
- `all_extended` selects every built-in report except `raw` and `graphql`.
- Unknown report names cause argument validation to fail.

## Raw REST reports

```bash
datascrape github --org my-org --report raw \
  --raw-scope org \
  --endpoint /orgs/{org}/actions/runners \
  --param per_page=100 \
  --extract-key runners \
  --output json
```

Supported placeholders:

- `{org}`
- `{enterprise}`
- `{repo}`
- `{full_name}`

Raw scopes: `org`, `repo`, or `enterprise`.

## GraphQL reports

Inline:

```bash
datascrape github --org my-org --report graphql \
  --query 'query { organization(login:"my-org") { login } }' \
  --output json
```

From a file:

```bash
datascrape github --org my-org --report graphql \
  --query-file ./query.graphql \
  --vars '{"org":"my-org"}' \
  --output json
```

## Output and joining

| Option | Default | Description |
|---|---|---|
| `--output` | `csv,json` | Comma-separated `csv,json,xlsx,html` |
| `--out-dir` | `./reports` | Export directory |
| `--columns` | unset | Ordered comma-separated output columns |
| `--enrich-csv` | unset | External CSV to join |
| `--enrich-key` | `repo_name` | Join key in the external CSV |
| `--redact-sensitive` | false | Redact sensitive fields |
| `--classification` | `confidential` | Manifest classification label |

Enrichment tries `full_name`, `repo`, `name`, and `identifier` against the enrichment lookup. External columns are prefixed with `ext_`.

## Scale and filters

| Option | Default | Description |
|---|---|---|
| `--batch` / `--chunk-size` | `50` | Items per batch/checkpoint |
| `--workers` / `--threads` | `8` | Thread pool size |
| `--limit` | unset | First N repositories per org |
| `--repo-filter` | unset | Regex against repo name/full name |
| `--skip-archived` | false | Exclude archived repositories |
| `--inactive-days` | `180` | Archived/inactive threshold |
| `--dry-run` | false | Preview scope and discovery without deep calls |
| `--custom-ci-files` | empty | Extra CI file paths |
| `--service-pattern` | unset | Custom bot/service account regex |

## Database and recovery

| Option | Default | Description |
|---|---|---|
| `--db-path` | unset | SQLite database file |
| `--db-backup` | false | Create backup after success |
| `--db-backup-dir` | `./backups` | Backup directory |
| `--db-retention-days` | unset | Prune old snapshots/checkpoints |
| `--backfill-since` | unset | ISO-8601 start boundary |
| `--backfill-until` | unset | ISO-8601 end boundary |
| `--resume` | false | Restore chunk checkpoints |
| `--retry-errors` | unset | Retry items from an errors CSV |

If `--resume` is set without `--db-path`, Datascrape uses `.datascrape_checkpoints.db` inside `--out-dir`.

## Network and TLS

| Option | Default | Description |
|---|---|---|
| `--timeout` | `30` | HTTP timeout in seconds |
| `--max-retries` | `8` | Maximum retries |
| `--backoff-base` | `2.0` | Initial backoff in seconds |
| `--backoff-max` | `900.0` | Maximum exponential delay |
| `--rate-buffer` | `10` | Remaining-request threshold |
| `--rate-check` | enabled | Run GitHub quota preflight |
| `--preflight` | enabled | Check organization visibility |
| `--ca-bundle` | unset | Corporate PEM/CRT CA bundle |
| `--insecure` | false | Disable TLS verification; not recommended |

## Logging and retention

| Option | Default | Description |
|---|---|---|
| `--log-dir` | `./logs` | Console-equivalent run logs |
| `--log-level` | environment or `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `--log-file` | generated | Filename override |
| `--no-file-log` | false | Console only |
| `--verbose` | false | Shortcut for DEBUG |
| `--log-retention-days` | unset | Prune old logs |
| `--run-dir` | `./runs` | JSON run manifests |
| `--run-retention-days` | unset | Prune old manifests |
| `--report-retention-days` | unset | Prune old reports |

Environment variable `DATASCRAPE_LOG_LEVEL` supplies the default log level.

## Combined configuration

Example `targets.json`:

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

Run:

```bash
datascrape combined --config targets.json --report repo_audit,archived_inactive --output csv,json
```
