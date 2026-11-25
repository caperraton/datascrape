# Docker and Compose

## Build

```bash
cp .env.example .env
docker compose build
```

Set credentials in `.env` rather than editing Compose or Dockerfiles.

## List reports

```bash
docker compose run --rm datascrape list-reports
```

## Example GitHub scan

```bash
docker compose run --rm datascrape github \
  --org my-org \
  --report repo_audit \
  --batch 50 \
  --workers 8 \
  --token ENV:GH_TOKEN \
  --output csv,json,xlsx,html \
  --out-dir /data/reports \
  --log-dir /data/logs \
  --run-dir /data/runs \
  --db-path /data/datascrape.db
```

## Combined scan

Place a combined config at `./config/targets.json`, which is mounted read-only at `/config`:

```bash
docker compose run --rm datascrape combined \
  --config /config/targets.json \
  --report repo_audit,archived_inactive \
  --out-dir /data/reports \
  --db-path /data/combined.db \
  --output csv,json
```

## GitHub App PEM

Place the PEM at `./certs/github-app.pem` and set `.env`:

```dotenv
GH_APP_ID=123456
GH_INSTALLATION_ID=789012
GH_APP_PRIVATE_KEY_FILE=/certs/github-app.pem
```

Pass it as:

```bash
docker compose run --rm datascrape github \
  --auth-type github_app \
  --app-id ENV:GH_APP_ID \
  --installation-id ENV:GH_INSTALLATION_ID \
  --private-key ENV:GH_APP_PRIVATE_KEY_FILE \
  --org my-org \
  --report repo_audit
```

## Persistent directories

The Compose service mounts:

| Host path | Container path | Purpose |
|---|---|---|
| `./data` | `/data` | Recommended database and report location |
| `./reports` | `/app/reports` | Default reports |
| `./logs` | `/app/logs` | Run logs |
| `./runs` | `/app/runs` | Run manifests |
| `./backups` | `/app/backups` | SQLite backups |
| `./config` | `/config:ro` | Combined-target JSON |
| `./certs` | `/certs:ro` | CA bundles and GitHub App PEM |

## Host permissions

The container uses UID/GID 1000 by default. On Linux, set `.env` to the host user's IDs:

```dotenv
DATASCRAPE_UID=1001
DATASCRAPE_GID=1001
```

Then rebuild or recreate the Compose service.

## Hardening

The Compose service:

- Runs with a non-root image user
- Drops all Linux capabilities
- Enables `no-new-privileges`
- Uses a read-only root filesystem
- Provides a read/write no-exec 128 MiB `/tmp` tmpfs
- Sets a 256-process PID limit
- Limits memory to 2 GiB
- Uses host bind mounts only for explicitly needed paths
- Requires no inbound ports

## Corporate CA bundle

Mount the bundle under `./certs`:

```dotenv
# No special .env entry is required for the mount
```

Run:

```bash
docker compose run --rm datascrape ghes \
  --url https://github.corp.com \
  --token ENV:GHES_TOKEN \
  --org my-org \
  --report repo_audit \
  --ca-bundle /certs/corporate-root.pem
```

## Compose troubleshooting

```bash
docker compose config
docker compose run --rm datascrape --help
docker compose logs --no-color
```

Use `docker compose run --rm --entrypoint sh datascrape` only for diagnosis; it can help verify mounted paths and environment variables inside the hardened container.
