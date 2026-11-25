# Authentication

## Credential resolution

Any CLI credential field may use `ENV:VARIABLE_NAME`. Datascrape resolves the variable when the client is created.

```bash
datascrape github --token ENV:GH_TOKEN --org my-org --report repo_audit
```

A combined config can use environment references:

```json
{
  "target": "github",
  "type": "cloud",
  "url": "https://api.github.com",
  "token": "ENV:GH_TOKEN",
  "auth_type": "bearer",
  "all_orgs": true
}
```

## GitHub Cloud and GHES

GitHub and GHES accept bearer tokens by default.

```bash
datascrape github --token ENV:GH_TOKEN --org my-org --report repo_audit

datascrape ghes --url https://github.corp.com --token ENV:GHES_TOKEN --org core-platform --report repo_audit
```

For GitHub Cloud, the personal access token must be authorized for SAML SSO when scanning SSO-enforced organizations.

## GitHub App

Provide:

- App ID
- Installation ID
- PEM private key path or PEM environment value

CLI example:

```bash
datascrape github \
  --auth-type github_app \
  --app-id ENV:GH_APP_ID \
  --installation-id ENV:GH_INSTALLATION_ID \
  --private-key /certs/github-app.pem \
  --org my-org \
  --report all
```

The private key can also be stored as a PEM environment variable:

```bash
$env:GH_APP_PRIVATE_KEY_FILE = "C:\certs\github-app.pem"
```

Datascrape creates a short-lived RS256 App JWT, exchanges it for an installation token, caches it until two minutes before expiry, and refreshes it automatically.

Keep private keys outside Git. The repository ignores `*.pem` and `*.key` files.

## Bitbucket Cloud

Bearer and basic authentication are supported. A Bearer workspace/repo access token can be used directly:

```bash
datascrape bitbucket \
  --type cloud \
  --url https://api.bitbucket.org/2.0 \
  --auth-type bearer \
  --token ENV:BB_TOKEN \
  --org my-workspace \
  --report repo_audit
```

Legacy basic authentication uses `username:app_password`:

```powershell
$env:BB_CREDS = "username:app_password"
```

```bash
datascrape bitbucket --auth-type basic --token ENV:BB_CREDS --org my-workspace --report repo_audit
```

## Bitbucket Server/Data Center

Use an HTTP access token as bearer authentication:

```bash
datascrape bitbucket \
  --type self_hosted \
  --url https://bitbucket.corp.com \
  --auth-type bearer \
  --token ENV:BB_TOKEN \
  --org PROJ1,PROJ2 \
  --report repo_audit
```

## Permission behavior

Datascrape records `ACCESS_DENIED` in applicable columns instead of stopping the whole run when a report operation lacks permission. Review error files and logs for the exact endpoint failures.

## Secret handling

- `--token ENV:...` keeps credential values out of shell arguments and config files.
- Run manifests redact token/private-key fields.
- Log filters redact common secret patterns.
- `--redact-sensitive` redacts common credential and sensitive fields from exports and checkpoints.
- `--classification` labels the run manifest; it does not itself encrypt or enforce access control.

See [Outputs and Data Storage](outputs-and-storage.md) for handling of generated confidential data.
