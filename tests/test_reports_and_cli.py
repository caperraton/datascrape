from __future__ import annotations

import threading
import time
from pathlib import Path

from datascrape.cli import _make_parser
from datascrape.reports import run_chunked


class FakeHTTP:
    total_requests = 0
    last_rate_remaining = "100"


def test_batch_and_thread_aliases():
    parser = _make_parser()
    args = parser.parse_args(["github", "--org", "acme", "--report", "repo_audit", "--batch", "17", "--threads", "5"])
    assert args.batch_size == 17
    assert args.workers == 5


def test_legacy_chunk_size_and_workers_aliases():
    parser = _make_parser()
    args = parser.parse_args(["github", "--org", "acme", "--report", "repo_audit", "--chunk-size", "9", "--workers", "3"])
    assert args.batch_size == 9
    assert args.workers == 3


def test_run_chunked_respects_batching_and_worker_limit():
    items = [{"full_name": f"org/repo-{index}"} for index in range(7)]
    active = 0
    maximum_active = 0
    lock = threading.Lock()

    def worker(item):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return {"repo": item["full_name"]}

    rows, errors = run_chunked(
        "test",
        items,
        worker,
        workers=2,
        chunk_size=3,
        http_client=FakeHTTP(),
    )
    assert len(rows) == 7
    assert not errors
    assert maximum_active <= 2


def test_interrupted_batch_saves_successful_checkpoints(tmp_path: Path):
    import pytest

    from datascrape.shutdown import clear_shutdown, request_shutdown, shutdown_requested
    from datascrape.storage import DBManager

    clear_shutdown()
    db = DBManager(tmp_path / "resume.db")
    items = [{"full_name": f"org/repo-{index}"} for index in range(3)]

    def worker(item):
        if item["full_name"].endswith("repo-0"):
            request_shutdown()
            return {"repo": item["full_name"]}
        if shutdown_requested():
            from datascrape.shutdown import ShutdownRequested
            raise ShutdownRequested("stop")
        return {"repo": item["full_name"]}

    try:
        with pytest.raises(KeyboardInterrupt):
            run_chunked("shutdown-test", items, worker, 1, 3, FakeHTTP(), state_db=db, run_key="resume-test")
        restored = db.get_completed_checkpoints("resume-test")
        assert restored == {"org/repo-0": [{"repo": "org/repo-0"}]}
    finally:
        clear_shutdown()
        db.close()


def test_cli_generates_timestamped_log_for_report_catalog(tmp_path: Path, capsys):
    from datascrape.cli import main

    main(["list-reports", "--log-dir", str(tmp_path), "--log-level", "DEBUG"])
    captured = capsys.readouterr()
    assert "REPORT NAME" in captured.out
    log_files = list(tmp_path.glob("datascrape_*.log"))
    assert len(log_files) == 1
    assert "Datascrape starting" in log_files[0].read_text(encoding="utf-8")


def test_cli_writes_outputs_logs_and_manifest_with_batch_settings(tmp_path: Path, monkeypatch):
    import json
    import datascrape.cli as cli_module

    class FakeClient:
        platform = "github"
        deployment_type = "cloud"
        base_url = "https://api.github.com"
        org_or_project = "acme"

        class HTTP:
            def get_rate_limit_state(self):
                return {"requests": 4, "retries": 1, "pauses": 0}

        http = HTTP()

        def close(self):
            self.closed = True

    fake_client = FakeClient()
    monkeypatch.setattr(cli_module, "build_client", lambda *args, **kwargs: fake_client)

    def fake_run_report(**kwargs):
        assert kwargs["chunk_size"] == 7
        assert kwargs["workers"] == 3
        return ([{"platform": "github", "org": "acme", "full_name": "acme/repo", "name": "repo"}], [], ["platform", "org", "full_name"])

    monkeypatch.setattr(cli_module, "run_report", fake_run_report)
    reports_dir = tmp_path / "reports"
    logs_dir = tmp_path / "logs"
    runs_dir = tmp_path / "runs"
    db_path = tmp_path / "data" / "state.db"
    cli_module.main(
        [
            "github",
            "--org",
            "acme",
            "--token",
            "test-token",
            "--report",
            "repo_audit",
            "--batch",
            "7",
            "--threads",
            "3",
            "--no-rate-check",
            "--no-preflight",
            "--output",
            "csv,json,xlsx,html",
            "--out-dir",
            str(reports_dir),
            "--log-dir",
            str(logs_dir),
            "--run-dir",
            str(runs_dir),
            "--db-path",
            str(db_path),
        ]
    )
    assert fake_client.closed is True
    assert (reports_dir / "github_repo_audit.csv").exists()
    assert (reports_dir / "github_repo_audit.xlsx").exists()
    assert list(reports_dir.glob("datascrape_dashboard_*.html"))
    manifest_path = next(runs_dir.glob("datascrape_*.json"))
    manifest = json.loads(manifest_path.read_text())
    assert manifest["status"] == "success"
    assert manifest["arguments"]["token"] == "[REDACTED]"
    assert manifest["arguments"]["batch_size"] == 7
    assert manifest["arguments"]["workers"] == 3
    assert manifest["summary"]["http_metrics"][0]["state"]["requests"] == 4
    assert list(logs_dir.glob("datascrape_*.log"))
