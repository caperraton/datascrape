# Datascrape Documentation

Datascrape is a concurrent, multi-platform CLI for repository audits, security and governance assessments, and engineering observability across GitHub Cloud, GitHub Enterprise Server, Bitbucket Cloud, and Bitbucket Server/Data Center.

## Documentation map

- [Getting Started](getting-started.md) — installation, credentials, and first scan
- [Usage Guide](usage.md) — CLI patterns, filtering, reports, and recovery
- [Authentication](authentication.md) — PATs, environment secrets, bearer/basic auth, and GitHub Apps
- [Report Catalog](reports.md) — all built-in reports and escape hatches
- [Architecture](architecture.md) — components, execution flow, concurrency, and data lifecycle
- [Docker and Compose](docker.md) — container setup, mounts, security, and common commands
- [Outputs and Data Storage](outputs-and-storage.md) — exports, manifests, SQLite, checkpoints, and retention
- [Configuration Reference](configuration.md) — combined-target JSON and CLI options
- [Troubleshooting](troubleshooting.md) — TLS, permissions, rate limits, Docker, and recovery
- [Development](development.md) — local setup, tests, linting, security checks, and contribution guidance

## Quick start

```bash
python -m pip install -e ".[dev]"
set GH_TOKEN=your_token_here
# PowerShell: $env:GH_TOKEN="your_token_here"

datascrape github --org my-org --report repo_audit --output csv,json
```

See [Getting Started](getting-started.md) for platform details and credential-safe setup.
