# Applying the Datascrape modernization

This archive is a complete source-tree snapshot based on the uploaded `datascrape-main.zip`. It moves the package and `pyproject.toml` from `datascrape1.1/` to the repository root and adds the modernization files.

## Apply it to an existing Git clone

1. Clone or back up your existing repository.
2. Extract this archive into the root of that clone, overwriting matching files.
3. Remove the old nested directory: `rm -rf datascrape1.1` (PowerShell: `Remove-Item -Recurse -Force .\datascrape1.1`).
4. Review `git status` and `git diff` before committing.
5. Do not add `.env`, PEM/private-key files, logs, reports, databases, or backups to Git.

## Validate locally

```bash
python -m pip install -e ".[dev]"
pre-commit install
pre-commit run --all-files
pytest -q
ruff check .
bandit -r datascrape -ll
pip-audit
```

## Docker

```bash
cp .env.example .env
docker compose build
docker compose run --rm datascrape list-reports
```

Set API credentials in `.env`, set `DATASCRAPE_UID`/`DATASCRAPE_GID` to your host user on Linux, mount a corporate CA bundle under `./certs`, and mount any combined target JSON under `./config`. The container runs as a non-root user and keeps reports, logs, run manifests, databases, and backups in persistent host-mounted directories.

## Repository owner action

The connected GitHub account used for this task has read-only permission on `caperraton/datascrape`; this archive has not been pushed and no pull request has been opened. After reviewing the changes, create a feature branch and push the commit using an account with repository write permission.

No license was added because the repository owner must choose the applicable licensing terms.
