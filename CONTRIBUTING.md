# Contributing

## Development setup

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
pre-commit install
```

## Before opening a pull request

```bash
ruff check .
ruff format --check .
pytest -q
bandit -r datascrape
pip-audit
```

Add tests for retry behavior, rate-limit handling, report transformations, and CLI changes. Do not commit credentials, private keys, real scan output, logs, databases, or customer data. Keep API calls mocked in unit tests; live integration tests must be explicitly opt-in and use a dedicated test organization.

## Security and data handling

Datascrape reports can contain confidential organization, access-control, integration, and security metadata. Treat generated reports as confidential by default, store them in access-controlled locations, and apply retention policies. Use `--redact-sensitive` where downstream consumers do not need credential-bearing values.

## Licensing

The repository owner must select and add a license before redistributing the project under an open-source license. This contribution does not assume or assign licensing terms.
