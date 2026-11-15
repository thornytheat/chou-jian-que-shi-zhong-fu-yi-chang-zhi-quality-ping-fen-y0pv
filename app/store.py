"""SQLite 访问：运行记录（含报告快照）与规则权重覆盖。

只有一个表 runs 和一个键值表 settings，够用即可，不引入 ORM。
报告整份以 JSON 存在 runs.report_json，重算权重时无需重新解析全部单元格。
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import threading

from . import settings

_LOCK = threading.Lock()
_CONN: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  source TEXT NOT NULL,
  status TEXT NOT NULL,
  stage TEXT NOT NULL,
  stage_index INTEGER NOT NULL DEFAULT 0,
  rows INTEGER NOT NULL DEFAULT 0,
  total_score REAL,
  grade TEXT,
  weight_overrides TEXT,
  report_json TEXT,
  error TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    global _CONN
    with _LOCK:
        if _CONN is None:
            settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
            _CONN = sqlite3.connect(settings.DB_PATH, check_same_thread=False)
            _CONN.row_factory = sqlite3.Row
            _CONN.execute("PRAGMA journal_mode=WAL")
            _CONN.executescript(SCHEMA)
            _CONN.commit()
        return _CONN


def reset_connection() -> None:
    """测试用：切换 DB 路径后重建连接。"""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
        _CONN = None


def now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def create_run(run_id: str, source: str, weight_overrides: dict | None) -> None:
    conn = connect()
    with _LOCK:
        conn.execute(
            "INSERT INTO runs (run_id, created_at, source, status, stage, stage_index, weight_overrides)"
            " VALUES (?,?,?,?,?,?,?)",
            (run_id, now(), source, "running", "排队中", 0,
             json.dumps(weight_overrides or {}, ensure_ascii=False)),
        )
        conn.commit()


def set_progress(run_id: str, stage_index: int, stage: str) -> None:
    conn = connect()
    with _LOCK:
        conn.execute(
            "UPDATE runs SET stage_index=?, stage=? WHERE run_id=?",
            (stage_index, stage, run_id),
        )
        conn.commit()


def finish_run(run_id: str, report: dict) -> None:
    conn = connect()
    score = report["score"]
    with _LOCK:
        conn.execute(
            "UPDATE runs SET status='done', stage='已完成', stage_index=99, rows=?,"
            " total_score=?, grade=?, report_json=? WHERE run_id=?",
            (report["dataset"]["rows"], score["total"], score["grade"],
             json.dumps(report, ensure_ascii=False), run_id),
        )
        conn.commit()


def fail_run(run_id: str, message: str) -> None:
    conn = connect()
    with _LOCK:
        conn.execute(
            "UPDATE runs SET status='failed', stage='失败', error=? WHERE run_id=?",
            (message, run_id),
        )
        conn.commit()


def get_run(run_id: str) -> dict | None:
    conn = connect()
    row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def list_runs(limit: int = 20) -> list[dict]:
    conn = connect()
    rows = conn.execute(
        "SELECT run_id, created_at, source, status, stage, rows, total_score, grade, error"
        " FROM runs ORDER BY created_at DESC, run_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def update_report(run_id: str, report: dict, weights: dict | None) -> None:
    conn = connect()
    score = report["score"]
    with _LOCK:
        conn.execute(
            "UPDATE runs SET total_score=?, grade=?, report_json=?, weight_overrides=? WHERE run_id=?",
            (score["total"], score["grade"], json.dumps(report, ensure_ascii=False),
             json.dumps(weights or {}, ensure_ascii=False), run_id),
        )
        conn.commit()


def count_runs() -> int:
    conn = connect()
    row = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
    return int(row["n"])


def _row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    raw = data.pop("report_json", None)
    data["report"] = json.loads(raw) if raw else None
    try:
        data["weight_overrides"] = json.loads(data.get("weight_overrides") or "{}")
    except json.JSONDecodeError:
        data["weight_overrides"] = {}
    return data


def get_setting(key: str, default: str | None = None) -> str | None:
    conn = connect()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    conn = connect()
    with _LOCK:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()


def weight_overrides() -> dict:
    raw = get_setting("weight_overrides", "{}") or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return {
        "columns": {k: float(v) for k, v in (data.get("columns") or {}).items()},
        "rule_weights": {k: float(v) for k, v in (data.get("rule_weights") or {}).items()},
    }


def save_weight_overrides(columns: dict, rule_weights: dict) -> dict:
    payload = {
        "columns": {k: float(v) for k, v in columns.items()},
        "rule_weights": {k: float(v) for k, v in rule_weights.items()},
    }
    set_setting("weight_overrides", json.dumps(payload, ensure_ascii=False))
    return payload
