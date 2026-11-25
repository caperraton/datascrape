# Security Policy

## Reporting a vulnerability

Do not open a public issue for a suspected security vulnerability. Contact the repository owner through GitHub's private vulnerability reporting feature, if enabled, or another private channel agreed with the maintainers.

## Data handling

Datascrape can collect organization membership, repository governance, security-alert metadata, integration endpoints, and audit information. Treat generated data as confidential by default. Restrict access to output directories, logs, SQLite databases, backups, and run manifests. Never commit API tokens, GitHub App private keys, real scan output, or production database files.

## Runtime recommendations

- Use fine-grained credentials and least-privilege permissions.
- Prefer GitHub App installation tokens for automated enterprise scans.
- Keep TLS verification enabled and provide corporate CA bundles where needed.
- Run the supplied container as its non-root user with the read-only root filesystem.
- Use `--redact-sensitive` when exported data does not need credential-bearing fields.
- Configure log, report, run-manifest, and database retention according to organizational policy.
- Review dependency and container scan results before releasing an image.
