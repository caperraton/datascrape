# Troubleshooting

## No report rows

Check:

1. Correct API URL and target type.
2. `--org` is the actual organization/workspace/project.
3. `--all-orgs` discovers accessible scopes.
4. `--limit`, `--repo-filter`, and `--skip-archived` are not excluding desired repositories.
5. GitHub SAML SSO authorization is present.
6. Error CSV files for `ACCESS_DENIED` or API exceptions.

Run a small discovery test:

```bash
datascrape github --org my-org --report repo_audit --limit 5 --dry-run
```

## TLS certificate errors

Use a corporate root CA bundle rather than disabling verification:

```bash
datascrape ghes --url https://github.corp.com --ca-bundle C:\certs\corp-root.pem --org my-org --report repo_audit
```

Docker mount example:

```bash
docker compose run --rm datascrape ghes \
  --url https://github.corp.com \
  --ca-bundle /certs/corp-root.pem \
  --org my-org --report repo_audit
```

Only use `--insecure` for controlled diagnosis when a CA bundle cannot be supplied.

## 401 unauthorized

- Confirm the credential environment variable exists.
- Confirm `ENV:VARIABLE_NAME` matches the exact variable name.
- For GitHub App auth, verify App ID, installation ID, private key, installation target, and API host.
- Confirm Bitbucket basic credentials use `username:app_password`.

## 403 or `ACCESS_DENIED`

The credential may lack permissions for one endpoint. Datascrape isolates those failures and continues.

Common checks:

- Repository or organization membership
- Fine-grained PAT repository and organization permissions
- GitHub App installation permissions
- SAML SSO authorization
- Enterprise/admin endpoints
- Bitbucket project/repository access

## Rate-limit delays

Datascrape reads GitHub quotas and coordinates shared pauses between workers.

If limits recur:

```bash
datascrape github --all-orgs --report repo_audit \
  --workers 4 --batch 25 --max-retries 8 \
  --backoff-base 2 --backoff-max 900
```

Keep `--resume` for long runs. Server reset and `Retry-After` directives take precedence over the configured backoff.

## GitHub App token errors

Verify:

- App ID and installation ID resolve to environment variables
- Private key is a valid PEM beginning with `-----BEGIN ... PRIVATE KEY-----`
- Private key belongs to the App
- Installation has access to the selected organization and repositories
- API URL is the correct GitHub Cloud or GHES host

## Docker reports not persisted

Use `/data` or mounted host directories:

```bash
docker compose run --rm datascrape github \
  --org my-org --report repo_audit \
  --token ENV:GH_TOKEN --out-dir /data/reports \
  --log-dir /data/logs --run-dir /data/runs \
  --db-path /data/datascrape.db
```

## Docker permission errors

On Linux set host IDs in `.env`:

```dotenv
DATASCRAPE_UID=1001
DATASCRAPE_GID=1001
```

Also verify host directory ownership and SELinux/AppArmor policies if applicable.

## Interrupted run

Use a database and resume:

```bash
datascrape github --all-orgs --report repo_audit \
  --db-path ./assessment.db --resume
```

If only repository errors remain:

```bash
datascrape github --all-orgs --report repo_audit \
  --retry-errors ./reports/github_repo_audit_errors.csv
```

## Corrupt or locked SQLite database

Datascrape performs WAL/busy-timeout setup and integrity checks. If a database is locked:

- Stop concurrent Datascrape processes using the database.
- Confirm the database directory is writable.
- Restore from a backup created with `--db-backup`.
- Use a separate database for concurrent independent scans.

List backups:

```text
./backups/datascrape_<run_id>.db
```

## Debug logging

```bash
datascrape github --org my-org --report repo_audit --verbose
```

Or set:

```powershell
$env:DATASCRAPE_LOG_LEVEL="DEBUG"
```

Use `--log-file` to override the generated log filename or `--no-file-log` for console-only logging.

## Secret accidentally exposed

- Revoke/rotate the credential.
- Remove real credentials from shell history and config files.
- Remove or rotate exposed private keys.
- Delete affected logs/reports or apply `--redact-sensitive` for future runs.
- Restore `.env` and PEM files from ignored locations only.
