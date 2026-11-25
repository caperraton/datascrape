import csv
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional, Set, Tuple


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
        print(f"[export] {len(json_rows)} rows -> {p}")

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
        print(f"[export] {len(csv_rows)} rows -> {p}")


class DBManager:
    """SQLite manager supporting upserts, historical snapshots, chunk checkpoints, and diffs."""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self) -> None:
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
        self.conn.commit()

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