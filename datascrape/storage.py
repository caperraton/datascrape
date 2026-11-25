import csv
import json
import logging
from html import escape
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


def flatten_dict(data: Dict[str, Any], parent_key: str = "", sep: str = ".") -> Dict[str, Any]:
    """Flatten nested dicts for clean CSV columns while preserving lists as JSON."""
    items: Dict[str, Any] = {}
    for k, v in data.items():
        if str(k).startswith("_"):
            continue
        new_key = f"{parent_key}{sep}{k}" if parent_key else str(k)
        if isinstance(v, dict):
            if not v:
                items[new_key] = ""
            else:
                items.update(flatten_dict(v, new_key, sep=sep))
        elif isinstance(v, list):
            if all(not isinstance(x, (dict, list)) for x in v):
                items[new_key] = ";".join(str(x) for x in v)
            else:
                items[new_key] = json.dumps(v, ensure_ascii=False)
        else:
            items[new_key] = v
    return items


def sanitize_row(row: Dict[str, Any], flatten: bool = False) -> Dict[str, Any]:
    if flatten:
        return flatten_dict(row)
    clean: Dict[str, Any] = {}
    for k, v in row.items():
        if str(k).startswith("_"):
            continue
        if isinstance(v, (dict, list)):
            clean[k] = json.dumps(v, ensure_ascii=False)
        else:
            clean[k] = v
    return clean


def filter_columns(rows: List[Dict[str, Any]], columns: Optional[List[str]]) -> List[Dict[str, Any]]:
    if not columns:
        return rows
    return [{c: r.get(c, "") for c in columns} for r in rows]


def redact_sensitive_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return copies of rows with common credential-bearing fields redacted."""
    sensitive_fragments = ("access_token", "refresh_token", "password", "secret_value", "client_secret", "private_key", "authorization", "webhook_url")

    def clean(value: Any, key: str = "") -> Any:
        normalized = key.lower().replace("-", "_")
        if any(fragment in normalized for fragment in sensitive_fragments) or normalized in {"token", "secret", "password"}:
            return "[REDACTED]"
        if isinstance(value, dict):
            return {str(child_key): clean(child_value, str(child_key)) for child_key, child_value in value.items()}
        if isinstance(value, list):
            return [clean(item, key) for item in value]
        if isinstance(value, str):
            import re
            value = re.sub(r"(?i)(gh[pousr]_[A-Za-z0-9_]{20,})", "[REDACTED_GITHUB_TOKEN]", value)
            value = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]", value)
            value = re.sub(r"(?i)([?&](?:access_token|token|api_key|password|secret)=)[^&\s]+", r"\1[REDACTED]", value)
            value = re.sub(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@", r"\1[REDACTED]@", value)
        return value

    return [clean(row) for row in rows]


def load_enrichment_csv(csv_path: Path, join_col: str) -> Dict[str, Dict[str, str]]:
    if not csv_path.exists():
        raise FileNotFoundError(f"Enrichment CSV not found: {csv_path}")
    lookup: Dict[str, Dict[str, str]] = {}
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            key = (row.get(join_col) or "").strip().lower()
            if not key:
                continue
            lookup[key] = {
                f"ext_{k.strip()}": (v or "").strip()
                for k, v in row.items()
                if k and k != join_col
            }
    return lookup


def enrich_rows(
    rows: List[Dict[str, Any]], lookup: Dict[str, Dict[str, str]], key_candidates: List[str]
) -> List[Dict[str, Any]]:
    if not lookup:
        return rows
    all_ext_cols = sorted({col for vals in lookup.values() for col in vals.keys()})
    enriched: List[Dict[str, Any]] = []
    for r in rows:
        match = None
        for cand in key_candidates:
            val = str(r.get(cand) or "").strip().lower()
            if val in lookup:
                match = lookup[val]
                break
        merged = dict(r)
        for col in all_ext_cols:
            merged[col] = match.get(col, "") if match else ""
        enriched.append(merged)
    return enriched


def export_report(
    rows: List[Dict[str, Any]],
    out_dir: Path,
    stem: str,
    formats: List[str],
    columns: Optional[List[str]] = None,
    flatten_csv: bool = True,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    if "json" in formats:
        json_rows = filter_columns([sanitize_row(r, flatten=False) for r in rows], columns)
        p = out_dir / f"{stem}.json"
        p.write_text(json.dumps(json_rows, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Exported %s JSON rows -> %s", len(json_rows), p)

    if "csv" in formats:
        csv_rows = filter_columns([sanitize_row(r, flatten=flatten_csv) for r in rows], columns)
        p = out_dir / f"{stem}.csv"
        fieldnames: List[str] = []
        seen: Set[str] = set()
        for r in csv_rows:
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    fieldnames.append(k)
        with p.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if fieldnames:
                writer.writeheader()
                writer.writerows(csv_rows)
        logger.info("Exported %s CSV rows -> %s", len(csv_rows), p)

    if "html" in formats:
        html_rows = filter_columns([sanitize_row(r, flatten=True) for r in rows], columns)
        fields: List[str] = []
        seen_fields: Set[str] = set()
        for row in html_rows:
            for key in row:
                if key not in seen_fields:
                    seen_fields.add(key)
                    fields.append(key)
        header = "".join(f"<th>{escape(str(field))}</th>" for field in fields)
        body_rows = []
        for row in html_rows:
            cells = "".join(f"<td>{escape(str(row.get(field, "") if row.get(field) is not None else ""))}</td>" for field in fields)
            body_rows.append(f"<tr>{cells}</tr>")
        document = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Datascrape report</title><style>
body{font-family:system-ui,sans-serif;margin:2rem;color:#17202a}h1{font-size:1.4rem}
.meta{color:#566573}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border:1px solid #d5d8dc;padding:7px;text-align:left;vertical-align:top}
th{background:#f2f4f4;position:sticky;top:0}tr:nth-child(even){background:#fafafa}
</style></head><body><h1>Datascrape report</h1>
<p class="meta">Rows: """ + str(len(html_rows)) + "</p><div style=\"overflow:auto\"><table><thead><tr>" + header + "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table></div></body></html>"
        p = out_dir / f"{stem}.html"
        p.write_text(document, encoding="utf-8")
        logger.info("Exported %s HTML rows -> %s", len(html_rows), p)

    if "xlsx" in formats:
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill
            from openpyxl.utils import get_column_letter
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise RuntimeError("Excel export requires openpyxl. Install datascrape with its reporting dependencies.") from exc
        excel_rows = filter_columns([sanitize_row(r, flatten=True) for r in rows], columns)
        fields = []
        seen_fields = set()
        for row in excel_rows:
            for key in row:
                if key not in seen_fields:
                    seen_fields.add(key)
                    fields.append(key)
        workbook = Workbook(write_only=False)
        base_sheet_name = "".join(char for char in (stem[:24] or "Report") if char not in "[]:*?/\\") or "Report"
        rows_per_sheet = 1_048_575
        chunks = [excel_rows[index:index + rows_per_sheet] for index in range(0, len(excel_rows), rows_per_sheet)] or [[]]
        for sheet_index, chunk in enumerate(chunks, start=1):
            sheet = workbook.active if sheet_index == 1 else workbook.create_sheet()
            sheet.title = base_sheet_name if sheet_index == 1 else f"{base_sheet_name}_{sheet_index}"[:31]
            if fields:
                sheet.append(fields)
                for cell in sheet[1]:
                    cell.font = Font(bold=True, color="FFFFFF")
                    cell.fill = PatternFill("solid", fgColor="263746")
                sheet.freeze_panes = "A2"
                for index, field in enumerate(fields, start=1):
                    sheet.column_dimensions[get_column_letter(index)].width = min(max(len(str(field)) + 2, 12), 48)
                for row in chunk:
                    sheet.append([row.get(field, "") for field in fields])
                sheet.auto_filter.ref = sheet.dimensions
        p = out_dir / f"{stem}.xlsx"
        workbook.save(p)
        logger.info("Exported %s Excel rows -> %s", len(excel_rows), p)


def export_run_dashboard(
    out_dir: Path,
    target: str,
    run_id: str,
    report_summary: Dict[str, Dict[str, Any]],
    outputs: List[str],
) -> Path:
    """Write a compact, self-contained HTML overview for a completed scan."""
    out_dir.mkdir(parents=True, exist_ok=True)
    total_rows = sum(int(item.get("rows", 0)) for item in report_summary.values())
    total_errors = sum(int(item.get("errors", 0)) for item in report_summary.values())
    report_rows = []
    for report, summary in report_summary.items():
        report_rows.append(
            "<tr>"
            f"<td>{escape(str(report))}</td>"
            f"<td>{int(summary.get('rows', 0))}</td>"
            f"<td>{int(summary.get('errors', 0))}</td>"
            f"<td>{int(summary.get('database_rows_upserted', 0))}</td>"
            "</tr>"
        )
    output_links = []
    for output in outputs:
        output_path = Path(output)
        if output_path.parent.resolve() == out_dir.resolve() and output_path.exists():
            output_links.append(f'<li><a href="{escape(output_path.name)}">{escape(output_path.name)}</a></li>')
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Datascrape run dashboard</title><style>'
        'body{font-family:system-ui,sans-serif;margin:2rem;color:#17202a}h1{font-size:1.5rem}'
        '.cards{display:flex;gap:1rem;flex-wrap:wrap}.card{padding:1rem;border:1px solid #d5d8dc;border-radius:8px;min-width:140px}'
        '.value{font-size:1.6rem;font-weight:700}table{border-collapse:collapse;width:100%;margin-top:1rem}'
        'th,td{border:1px solid #d5d8dc;padding:8px;text-align:left}th{background:#f2f4f4}'
        '</style></head><body>'
        f'<h1>Datascrape run dashboard</h1><p>Target: {escape(target)} · Run: {escape(run_id)}</p>'
        f'<div class="cards"><div class="card">Total rows<div class="value">{total_rows}</div></div>'
        f'<div class="card">Errors<div class="value">{total_errors}</div></div>'
        f'<div class="card">Reports<div class="value">{len(report_summary)}</div></div></div>'
        '<h2>Report summary</h2><table><thead><tr><th>Report</th><th>Rows</th><th>Errors</th><th>SQLite upserts</th></tr></thead><tbody>'
        + "".join(report_rows)
        + '</tbody></table><h2>Generated files</h2><ul>'
        + "".join(output_links)
        + '</ul></body></html>'
    )
    path = out_dir / f"datascrape_dashboard_{run_id}.html"
    path.write_text(document, encoding="utf-8")
    logger.info("Run dashboard created: %s", path)
    return path


class DBManager:
    """SQLite manager supporting upserts, historical snapshots, chunk checkpoints, and diffs."""

    SCHEMA_VERSION = 1

    def __init__(self, db_path: Path, timeout: int = 30):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, timeout=timeout)
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        current_version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if current_version > self.SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {current_version} is newer than this Datascrape version supports ({self.SCHEMA_VERSION})."
            )
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS report_records (
                report_type TEXT NOT NULL,
                record_key TEXT NOT NULL,
                platform TEXT NOT NULL,
                org TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (report_type, record_key)
            );
            CREATE TABLE IF NOT EXISTS report_snapshots (
                run_id TEXT NOT NULL,
                report_type TEXT NOT NULL,
                record_key TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (run_id, report_type, record_key)
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                run_key TEXT NOT NULL,
                item_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (run_key, item_id)
            );
        """)
        self.conn.execute("PRAGMA user_version = 1")
        self.conn.commit()
        logger.info("SQLite database ready: %s (schema v%s)", self.db_path, self.SCHEMA_VERSION)

    def integrity_check(self) -> str:
        result = self.conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {result}")
        return str(result)

    def prune_history(self, retention_days: int) -> Dict[str, int]:
        if retention_days < 1:
            raise ValueError("Database retention must be at least one day")
        cutoff = f"-{int(retention_days)} days"
        snapshots = self.conn.execute(
            "DELETE FROM report_snapshots WHERE captured_at < datetime('now', ?)", (cutoff,)
        ).rowcount
        checkpoints = self.conn.execute(
            "DELETE FROM checkpoints WHERE completed_at < datetime('now', ?)", (cutoff,)
        ).rowcount
        self.conn.commit()
        logger.info("Database retention pruned %s snapshots and %s checkpoints older than %s days", snapshots, checkpoints, retention_days)
        return {"snapshots": snapshots, "checkpoints": checkpoints}

    def backup(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(destination)
        try:
            self.conn.backup(target)
        finally:
            target.close()
        logger.info("SQLite backup created: %s", destination)
        return destination

    def get_completed_checkpoints(self, run_key: str) -> Dict[str, List[Dict[str, Any]]]:
        cur = self.conn.execute("SELECT item_id, payload_json FROM checkpoints WHERE run_key = ?", (run_key,))
        return {item_id: json.loads(payload) for item_id, payload in cur.fetchall()}

    def save_checkpoint_batch(self, run_key: str, item_results: Dict[str, List[Dict[str, Any]]]) -> None:
        cur = self.conn.cursor()
        for item_id, rows in item_results.items():
            clean = [sanitize_row(r, flatten=False) for r in rows]
            cur.execute(
                """
                INSERT INTO checkpoints (run_key, item_id, payload_json, completed_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(run_key, item_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    completed_at = CURRENT_TIMESTAMP
                """,
                (run_key, item_id, json.dumps(clean)),
            )
        self.conn.commit()

    def upsert_rows(
        self, report_type: str, rows: List[Dict[str, Any]], key_fields: List[str], run_id: str, save_snapshot: bool = True
    ) -> int:
        cur = self.conn.cursor()
        count = 0
        for raw_row in rows:
            r = sanitize_row(raw_row, flatten=False)
            key = "|".join(str(r.get(k, "")) for k in key_fields) or f"row_{count}"
            payload = json.dumps(r)
            cur.execute(
                """
                INSERT INTO report_records (report_type, record_key, platform, org, payload_json, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(report_type, record_key) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (report_type, key, str(r.get("platform", "")), str(r.get("org", "")), payload),
            )
            if save_snapshot:
                cur.execute(
                    """
                    INSERT OR REPLACE INTO report_snapshots (run_id, report_type, record_key, payload_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (run_id, report_type, key, payload),
                )
            count += 1
        self.conn.commit()
        return count

    def list_runs(self, report_type: str) -> List[Tuple[str, int]]:
        cur = self.conn.execute(
            """
            SELECT run_id, COUNT(*)
            FROM report_snapshots
            WHERE report_type = ?
            GROUP BY run_id
            ORDER BY run_id ASC
            """,
            (report_type,),
        )
        return cur.fetchall()

    def report_trends(self, report_type: str) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            """
            SELECT run_id, COUNT(*) AS record_count,
                   MIN(captured_at) AS first_captured_at,
                   MAX(captured_at) AS last_captured_at
            FROM report_snapshots
            WHERE report_type = ?
            GROUP BY run_id
            ORDER BY run_id ASC
            """,
            (report_type,),
        )
        return [
            {
                "report_type": report_type,
                "run_id": row[0],
                "record_count": row[1],
                "first_captured_at": row[2],
                "last_captured_at": row[3],
            }
            for row in cur.fetchall()
        ]

    def diff_snapshots(self, report_type: str, from_run: str, to_run: str) -> List[Dict[str, Any]]:
        def _load(run_id: str) -> Dict[str, Dict[str, Any]]:
            cur = self.conn.execute(
                "SELECT record_key, payload_json FROM report_snapshots WHERE report_type = ? AND run_id = ?",
                (report_type, run_id),
            )
            return {k: json.loads(v) for k, v in cur.fetchall()}

        old_map = _load(from_run)
        new_map = _load(to_run)
        changes: List[Dict[str, Any]] = []

        for key in sorted(set(old_map.keys()) | set(new_map.keys())):
            if key not in old_map:
                changes.append({"record_key": key, "change_type": "ADDED", "changed_fields": "", "from_run": from_run, "to_run": to_run})
            elif key not in new_map:
                changes.append({"record_key": key, "change_type": "REMOVED", "changed_fields": "", "from_run": from_run, "to_run": to_run})
            else:
                old_row, new_row = old_map[key], new_map[key]
                diffs = []
                for field in sorted(set(old_row.keys()) | set(new_row.keys())):
                    if old_row.get(field) != new_row.get(field):
                        diffs.append(f"{field}: {old_row.get(field)!r} -> {new_row.get(field)!r}")
                if diffs:
                    changes.append({
                        "record_key": key,
                        "change_type": "MODIFIED",
                        "changed_fields": " | ".join(diffs),
                        "from_run": from_run,
                        "to_run": to_run,
                    })
        return changes

    def close(self) -> None:
        self.conn.close()