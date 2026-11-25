# Changelog

## 1.1.0 - 2026-11-25

- Move the Python package and packaging metadata to the repository root.
- Add shared rate-limit coordination, GitHub `/rate_limit` preflight, reset-header and `Retry-After` handling, exponential backoff with jitter, and transient request retries.
- Add `--batch` and `--threads` aliases while preserving `--chunk-size` and `--workers`.
- Add standard-library console/file logging, secret redaction, timestamped log files, and JSON run manifests.
- Add GitHub App installation-token authentication.
- Add HTML and Excel report exports, SQLite integrity checks, history retention, and backups.
- Add hardened Docker/Compose configuration, pre-commit, CI, security scanning, Dependabot, and release automation.
