# Report Catalog

Run `datascrape list-reports` to print the report catalog for the installed version.

Report scope is `repo`, `org`, `enterprise`, or `dynamic` for raw REST data.

## Core assessment and governance

| Report | Scope | Description |
|---|---|---|
| `ownership_baseline` | repo | CODEOWNERS, custom owner properties, CI detection, and business-area rollups |
| `apps_service_accounts` | org | GitHub Apps, installers, permissions, events, and candidate bot/service accounts |
| `archived_inactive` | repo | Archived and stale repositories, size, and disposition |
| `repo_audit` | repo | Size, LFS, Actions, secrets, branch protections, rulesets, teams, users, and admins |
| `repo_configurations` | repo | Repository settings, GHAS, security features, webhooks, governance files |
| `enterprise_policies` | enterprise | Enterprise creation, 2FA, SSO, IP allow lists, Actions, rulesets, security configs, Copilot, and licensing |

The `all` shortcut expands to the first five core reports. Enterprise policies are selected separately when they are required.

## Inventory and IAM

| Report | Scope | Description |
|---|---|---|
| `languages` | repo | Language percentages and byte counts |
| `releases_tags` | repo | Releases, latest tag, tags, and binary asset sizes |
| `submodules` | repo | `.gitmodules` submodule paths and targets |
| `teams_members` | org | Teams, hierarchy, privacy, permissions, and members |
| `webhooks_deploy_keys` | repo | Webhooks, events, SSL flags, and deploy keys |
| `iam_tokens_sso` | org | Fine-grained PAT approval and SAML credential authorization |
| `dormant_users` | org | Members correlated with recent organization events |

## Security and compliance

| Report | Scope | Description |
|---|---|---|
| `security_alerts` | org | Code scanning, secret scanning, and Dependabot alerts |
| `org_secrets_variables` | org | Organization Actions secrets and variables |
| `sbom_licenses` | repo | SPDX licenses and dependency SBOM/package counts |
| `actions_pinning` | repo | Workflow actions not pinned to full commit SHAs |
| `audit_log` | org | Organization audit log, optionally bounded by backfill timestamps |

## CI/CD and observability

| Report | Scope | Description |
|---|---|---|
| `runners_packages` | org | Runners, Actions cache use, and packages |
| `environments` | repo | Deployment environments, protection rules, and secret names |
| `workflow_runs` | repo | Pass/fail, average duration, and p95 duration |
| `pages` | repo | Pages status, URL, custom domain, and HTTPS |
| `pull_request_metrics` | repo | PR state, timestamps, merge data, and cycle time |
| `pr_reviews` | repo | Time-to-first-review, approvals, and reviewers |
| `contributors_bus_factor` | repo | Top contributor share and bus-factor risk |
| `issues_sla` | repo | Issue backlog, labels, and close-time SLA |
| `dora_metrics` | repo | Deployments, releases, merged PRs, and lead time |
| `billing_copilot` | org | Actions/packages/storage, GHAS, Copilot, and Codespaces |

## Universal escape hatches

### Raw REST

```bash
datascrape github --org my-org --report raw \
  --raw-scope org \
  --endpoint /orgs/{org}/actions/runners \
  --param per_page=100 \
  --extract-key runners \
  --output json
```

Raw can use `--raw-scope org`, `repo`, or `enterprise` and supports the placeholders `{org}`, `{enterprise}`, `{repo}`, and `{full_name}`.

For repo-scoped raw endpoints, Datascrape processes repositories in the normal discovery/filter/chunk pipeline.

### GraphQL

```bash
datascrape github --org my-org --report graphql \
  --query-file ./query.graphql \
  --vars '{"org":"my-org"}' \
  --output json
```

GraphQL is currently implemented on GitHub/GHES clients.

## Platform support

Some report methods provide graceful cross-platform fallbacks. Unsupported operations may return `NOT_SUPPORTED`, empty result sets, or base rows with zero/empty metrics. The report catalog should be treated as a GitHub-focused catalog; GitHub and GHES generally offer the richest coverage.
