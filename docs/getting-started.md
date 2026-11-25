# Getting Started

## Prerequisites

- Python 3.10 or newer
- Network access to the source-control API
- A PAT or GitHub App installation with permissions for the selected reports
- Docker Engine and Compose, optional, for containerized runs

## Install locally

From the repository root:

```bash
python -m venv .venv
```

Activate it:

```text
# Windows PowerShell
.venv\Scripts\Activate.ps1

# Windows Command Prompt
.venv\Scripts\activate.bat

# macOS/Linux
source .venv/bin/activate
```

Install the package and development tools:

```bash
python -m pip install -e ".[dev]"
```

Verify the installation:

```bash
datascrape --help
datascrape list-reports
```

You can also invoke the module directly:

```bash
python -m datascrape.cli list-reports
```

## Set credentials safely

Do not place credentials directly in command history or JSON files. Export an environment variable and reference it with `ENV:VARIABLE_NAME`.

PowerShell:

```powershell
$env:GH_TOKEN = "your-github-token"
```

Windows Command Prompt:

```cmd
set GH_TOKEN=your-github-token
```

macOS/Linux:

```bash
export GH_TOKEN="your-github-token"
```

## Run a smoke test

```bash
datascrape github \
  --type cloud \
  --url https://api.github.com \
  --token ENV:GH_TOKEN \
  --org my-org \
  --report repo_audit,repo_configurations \
  --limit 5 \
  --output csv,json
```

A full run example:

```bash
datascrape github \
  --token ENV:GH_TOKEN \
  --all-orgs \
  --report all_extended \
  --batch 50 \
  --workers 8 \
  --db-path ./assessment.db \
  --resume \
  --out-dir ./reports
```

## First-run workflow

1. Run `datascrape list-reports` to review available reports.
2. Start with `--limit 5` and a core report.
3. Confirm org visibility and API permissions.
4. Add `--dry-run` to preview discovered repositories without deep API calls.
5. Run the selected assessment with bounded concurrency.
6. Review CSV/JSON/HTML exports, error files, logs, and the run manifest.
7. Use SQLite and `diff`/`trends` for recurring assessments.

## Minimal Docker start

```bash
cp .env.example .env
# Add credentials to .env

docker compose build
docker compose run --rm datascrape list-reports
```

Continue with [Docker and Compose](docker.md).
