from __future__ import annotations

import json
import logging
from pathlib import Path

from openpyxl import load_workbook

from datascrape.logging_utils import configure_logging
from datascrape.run_metadata import write_run_manifest
from datascrape.storage import DBManager, export_report, redact_sensitive_rows


def test_logging_creates_timestamped_file_and_redacts_secrets(tmp_path: Path):
    log_path = configure_logging(tmp_path, level="DEBUG")
    assert log_path is not None
    logging.getLogger("test.secret").warning("Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz1234567890 token=topsecret")
    try:
        raise RuntimeError("https://user:password@example.test/?api_key=private")
    except RuntimeError:
        logging.getLogger("test.secret").exception("request failed")
    for handler in logging.getLogger().handlers:
        handler.flush()
    content = log_path.read_text(encoding="utf-8")
    assert "[REDACTED]" in content
    assert "topsecret" not in content
    assert "ghp_abcdefghijklmnopqrstuvwxyz1234567890" not in content
    assert "user:password" not in content
    assert "api_key=private" not in content
    assert log_path.name.startswith("datascrape_")


def test_export_report_writes_csv_json_html_and_xlsx(tmp_path: Path):
    rows = [{"repo": "example", "settings": {"visibility": "private"}, "count": 3}]
    export_report(rows, tmp_path, "sample", ["csv", "json", "html", "xlsx"])
    assert (tmp_path / "sample.csv").exists()
    assert json.loads((tmp_path / "sample.json").read_text())[0]["repo"] == "example"
    html = (tmp_path / "sample.html").read_text()
    assert "example" in html and "settings.visibility" in html
    workbook = load_workbook(tmp_path / "sample.xlsx", read_only=True)
    sheet = workbook.active
    assert sheet.max_row == 2
    assert sheet.cell(2, 1).value == "example"


def test_sensitive_field_redaction():
    rows = [{"repo": "safe", "access_token": "secret", "webhook_url": "https://example.test/hook"}]
    redacted = redact_sensitive_rows(rows)
    assert redacted[0]["repo"] == "safe"
    assert redacted[0]["access_token"] == "[REDACTED]"
    assert redacted[0]["webhook_url"] == "[REDACTED]"
    assert rows[0]["access_token"] == "secret"


def test_sqlite_integrity_backup_and_history_retention(tmp_path: Path):
    db_path = tmp_path / "data" / "state.db"
    db = DBManager(db_path)
    db.upsert_rows("repo_audit", [{"platform": "github", "org": "acme", "repo": "one"}], ["platform", "org", "repo"], "run-1")
    assert db.integrity_check() == "ok"
    backup = db.backup(tmp_path / "backup" / "state.db")
    assert backup.exists()
    result = db.prune_history(1)
    assert result == {"snapshots": 0, "checkpoints": 0}
    db.close()


def test_run_manifest_redacts_credentials(tmp_path: Path):
    path = write_run_manifest(
        tmp_path / "runs" / "run.json",
        run_id="abc",
        status="success",
        started_at="2026-11-25T00:00:00+00:00",
        finished_at="2026-11-25T00:01:00+00:00",
        arguments={"token": "ghp_secret", "private_key": "pem", "workers": 4, "url": "https://user:password@example.test/api?token=secret"},
        reports=["repo_audit"],
        outputs=[],
        summary={"rows": 1},
        log_file=None,
        error="request failed for https://user:password@example.test/?token=secret",
    )
    payload = json.loads(path.read_text())
    assert payload["arguments"]["token"] == "[REDACTED]"
    assert payload["arguments"]["private_key"] == "[REDACTED]"
    assert payload["arguments"]["workers"] == 4
    assert payload["arguments"]["url"] == "https://example.test/api"
    assert "user:password" not in payload["error"]
    assert "token=secret" not in payload["error"]


def test_run_dashboard_contains_report_counts_and_output_links(tmp_path: Path):
    from datascrape.storage import export_run_dashboard

    report_file = tmp_path / "github_repo_audit.csv"
    report_file.write_text("repo\nexample\n")
    dashboard = export_run_dashboard(
        tmp_path,
        "github",
        "run-123",
        {"repo_audit": {"rows": 1, "errors": 0, "database_rows_upserted": 1}},
        [str(report_file)],
    )
    content = dashboard.read_text()
    assert "Total rows" in content
    assert "repo_audit" in content
    assert "github_repo_audit.csv" in content


def test_database_historical_trends_and_retention(tmp_path: Path):
    db = DBManager(tmp_path / "trends.db")
    row = {"platform": "github", "org": "acme", "full_name": "acme/repo"}
    db.upsert_rows("repo_audit", [row], ["platform", "org", "full_name"], "run-001")
    db.upsert_rows("repo_audit", [row, {**row, "full_name": "acme/repo-2"}], ["platform", "org", "full_name"], "run-002")
    trends = db.report_trends("repo_audit")
    assert [point["record_count"] for point in trends] == [1, 2]
    db.conn.execute("UPDATE report_snapshots SET captured_at = datetime('now', '-3 days') WHERE run_id = 'run-001'")
    db.conn.commit()
    result = db.prune_history(1)
    assert result["snapshots"] == 1
    db.close()
