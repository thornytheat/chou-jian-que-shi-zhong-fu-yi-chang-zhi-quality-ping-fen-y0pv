"""SQLite 访问：运行记录、分片检查点、隔离记录、发布指针与规则权重覆盖。

表设计（不引入 ORM）：
- runs：一次抽检作业的主记录，报告整份 JSON 存 report_json；publish_status 标识是否已发布。
- job_shards：每片一个检查点（pending/processing/confirmed/failed），重试只重做未确认片，
  已确认片的健康行以行级事务落库，重试不会重复计数。
- quarantine_records：坏记录隔离区，每条带原因码与中文说明。
- settings：键值表（权重覆盖、seed 版本、当前已发布 run_id）。

发布是原子的：全部片确认且「源记录数 = 健康 + 隔离」核对相符后，才把 publish_status 置为
published 并切换 published_run_id；失败期间旧发布指针保持不变，页面与工单继续读到旧版。
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
  source_rows INTEGER NOT NULL DEFAULT 0,
  healthy_rows INTEGER NOT NULL DEFAULT 0,
  quarantine_rows INTEGER NOT NULL DEFAULT 0,
  shard_count INTEGER NOT NULL DEFAULT 0,
  shard_size INTEGER NOT NULL DEFAULT 0,
  confirmed_shards INTEGER NOT NULL DEFAULT 0,
  publish_status TEXT NOT NULL DEFAULT 'unpublished',
  published_at TEXT,
  total_score REAL,
  grade TEXT,
  weight_overrides TEXT,
  intake_json TEXT,
  report_json TEXT,
  error TEXT
);
CREATE TABLE IF NOT EXISTS job_shards (
  run_id TEXT NOT NULL,
  shard_index INTEGER NOT NULL,
  status TEXT NOT NULL,
  total_records INTEGER NOT NULL DEFAULT 0,
  healthy_records INTEGER NOT NULL DEFAULT 0,
  quarantine_records INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  confirmed_at TEXT,
  rows_json TEXT,
  PRIMARY KEY (run_id, shard_index)
);
CREATE TABLE IF NOT EXISTS quarantine_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  shard_index INTEGER NOT NULL,
  line_no INTEGER NOT NULL,
  row_id TEXT,
  reason_code TEXT NOT NULL,
  reason TEXT NOT NULL,
  raw_text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quarantine_run ON quarantine_records(run_id);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# 旧库（v1 只有 runs 基础列）增量加列，已存在则忽略
_MIGRATION_COLUMNS = [
    ("runs", "source_rows", "INTEGER NOT NULL DEFAULT 0"),
    ("runs", "healthy_rows", "INTEGER NOT NULL DEFAULT 0"),
    ("runs", "quarantine_rows", "INTEGER NOT NULL DEFAULT 0"),
    ("runs", "shard_count", "INTEGER NOT NULL DEFAULT 0"),
    ("runs", "shard_size", "INTEGER NOT NULL DEFAULT 0"),
    ("runs", "confirmed_shards", "INTEGER NOT NULL DEFAULT 0"),
    ("runs", "publish_status", "TEXT NOT NULL DEFAULT 'unpublished'"),
    ("runs", "published_at", "TEXT"),
    ("runs", "intake_json", "TEXT"),
]


def connect() -> sqlite3.Connection:
    global _CONN
    with _LOCK:
        if _CONN is None:
            settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
            _CONN = sqlite3.connect(settings.DB_PATH, check_same_thread=False)
            _CONN.row_factory = sqlite3.Row
            _CONN.execute("PRAGMA journal_mode=WAL")
            _CONN.executescript(SCHEMA)
            _migrate(_CONN)
            _CONN.commit()
        return _CONN


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    for table, column, decl in _MIGRATION_COLUMNS:
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def reset_connection() -> None:
    """测试用：切换 DB 路径后重建连接。"""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
        _CONN = None


def now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 运行主记录


def create_run(run_id: str, source: str, weight_overrides: dict | None,
               intake: dict | None = None) -> None:
    conn = connect()
    with _LOCK:
        conn.execute(
            "INSERT INTO runs (run_id, created_at, source, status, stage, stage_index,"
            " source_rows, healthy_rows, quarantine_rows, weight_overrides, intake_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, now(), source, "running", "排队中", 0,
             (intake or {}).get("source_count", 0),
             (intake or {}).get("healthy_count", 0),
             (intake or {}).get("quarantine_count", 0),
             json.dumps(weight_overrides or {}, ensure_ascii=False),
             json.dumps(intake, ensure_ascii=False) if intake else None),
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


def init_shards(run_id: str, shard_size: int, shard_summaries: list[dict]) -> None:
    """创建作业时一次性写入全部分片的 pending 检查点（含每片源/健康/隔离计数预算）。"""
    conn = connect()
    with _LOCK:
        conn.execute(
            "UPDATE runs SET shard_count=?, shard_size=?, stage='分片导入中' WHERE run_id=?",
            (len(shard_summaries), shard_size, run_id),
        )
        conn.executemany(
            "INSERT INTO job_shards (run_id, shard_index, status, total_records,"
            " healthy_records, quarantine_records, attempts)"
            " VALUES (?,?, 'pending', ?, ?, ?, 0)",
            [
                (run_id, s["shard_index"], s["total_records"],
                 s["healthy_records"], s["quarantine_records"])
                for s in shard_summaries
            ],
        )
        conn.commit()


def mark_shard_processing(run_id: str, shard_index: int) -> None:
    conn = connect()
    with _LOCK:
        conn.execute(
            "UPDATE job_shards SET status='processing', attempts=attempts+1 WHERE run_id=? AND shard_index=?",
            (run_id, shard_index),
        )
        conn.commit()


def confirm_shard(run_id: str, shard_index: int, healthy_rows: list[dict],
                  quarantine_rows: list[dict]) -> None:
    """行级事务确认一片：健康行与隔离记录一起落库，失败整体回滚（不留半片、不重复计数）。"""
    conn = connect()
    with _LOCK:
        try:
            conn.execute(
                "UPDATE job_shards SET status='confirmed', rows_json=?, last_error=NULL,"
                " confirmed_at=? WHERE run_id=? AND shard_index=?",
                (json.dumps(healthy_rows, ensure_ascii=False), now(), run_id, shard_index),
            )
            conn.executemany(
                "INSERT INTO quarantine_records (run_id, shard_index, line_no, row_id,"
                " reason_code, reason, raw_text) VALUES (?,?,?,?,?,?,?)",
                [
                    (run_id, shard_index, item["line_no"], item.get("row_id"),
                     item["reason_code"], item["reason"], item["raw_text"])
                    for item in quarantine_rows
                ],
            )
            conn.execute(
                "UPDATE runs SET confirmed_shards=("
                "SELECT COUNT(*) FROM job_shards WHERE run_id=? AND status='confirmed')"
                " WHERE run_id=?",
                (run_id, run_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def fail_shard(run_id: str, shard_index: int, message: str, stage: str | None = None) -> None:
    conn = connect()
    with _LOCK:
        conn.execute(
            "UPDATE job_shards SET status='failed', last_error=? WHERE run_id=? AND shard_index=?",
            (message, run_id, shard_index),
        )
        conn.execute(
            "UPDATE runs SET status='failed', stage=?, error=? WHERE run_id=?",
            (stage or f"第 {shard_index + 1} 片失败", message, run_id),
        )
        conn.commit()


def reset_failed_shards(run_id: str) -> int:
    """续跑前把失败片（含线程中断留下的 processing 片）退回 pending；confirmed 片不动，因此不会重复计数。"""
    conn = connect()
    with _LOCK:
        cur = conn.execute(
            "UPDATE job_shards SET status='pending', last_error=NULL"
            " WHERE run_id=? AND status IN ('failed', 'processing')",
            (run_id,),
        )
        conn.execute(
            "UPDATE runs SET status='running', stage='分片续跑中', error=NULL WHERE run_id=?",
            (run_id,),
        )
        conn.commit()
        return cur.rowcount


def list_shards(run_id: str) -> list[dict]:
    conn = connect()
    rows = conn.execute(
        "SELECT run_id, shard_index, status, total_records, healthy_records,"
        " quarantine_records, attempts, last_error, confirmed_at"
        " FROM job_shards WHERE run_id=? ORDER BY shard_index",
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def load_confirmed_rows(run_id: str) -> list[dict]:
    """读取全部已确认片的健康行（顺序按分片号，片内保持原始顺序）。"""
    conn = connect()
    rows = conn.execute(
        "SELECT shard_index, rows_json FROM job_shards"
        " WHERE run_id=? AND status='confirmed' ORDER BY shard_index",
        (run_id,),
    ).fetchall()
    result: list[dict] = []
    for r in rows:
        result.extend(json.loads(r["rows_json"] or "[]"))
    return result


def finish_run(run_id: str, report: dict) -> None:
    """分析与合并完成：报告落库，但仍处于 unpublished，等核对与统一发布。"""
    conn = connect()
    score = report["score"]
    with _LOCK:
        conn.execute(
            "UPDATE runs SET status='done', stage='待发布（核对中）', stage_index=99,"
            " rows=?, healthy_rows=?, quarantine_rows=?,"
            " total_score=?, grade=?, report_json=? WHERE run_id=?",
            (report["dataset"]["rows"], report["dataset"].get("healthy_rows", report["dataset"]["rows"]),
             report["dataset"].get("quarantine_rows", 0),
             score["total"], score["grade"],
             json.dumps(report, ensure_ascii=False), run_id),
        )
        conn.commit()


def reconcile_and_publish(run_id: str) -> dict:
    """发布前最后一道闸：源记录数 = 健康 + 隔离；相符才原子发布并切换发布指针。

    返回核对明细；不符时抛 ReconcileError，调用方保留旧版发布不变。
    """
    conn = connect()
    with _LOCK:
        run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise ReconcileError(f"运行记录 {run_id} 不存在。")
        if run["status"] != "done":
            raise ReconcileError("作业尚未完成全部处理，不能发布。")
        shard_rows = conn.execute(
            "SELECT COALESCE(SUM(total_records),0) AS total,"
            " COALESCE(SUM(healthy_records),0) AS healthy,"
            " COALESCE(SUM(quarantine_records),0) AS quarantined,"
            " SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END) AS confirmed,"
            " COUNT(*) AS shards FROM job_shards WHERE run_id=?",
            (run_id,),
        ).fetchone()
        actual_quarantine = conn.execute(
            "SELECT COUNT(*) AS n FROM quarantine_records WHERE run_id=?", (run_id,),
        ).fetchone()["n"]

        source = run["source_rows"]
        healthy = run["healthy_rows"]
        quarantined = run["quarantine_rows"]
        detail = {
            "source": source,
            "healthy": healthy,
            "quarantined": quarantined,
            "actual_quarantine_rows": actual_quarantine,
            "shard_total": shard_rows["total"],
            "shard_healthy": shard_rows["healthy"],
            "shard_quarantined": shard_rows["quarantined"],
            "confirmed_shards": shard_rows["confirmed"],
            "shard_count": shard_rows["shards"],
        }
        problems = []
        if shard_rows["confirmed"] != shard_rows["shards"]:
            problems.append(
                f"仍有未确认分片（已确认 {shard_rows['confirmed']}/{shard_rows['shards']}）")
        if shard_rows["total"] != source:
            problems.append(f"分片源记录合计 {shard_rows['total']} ≠ 作业源记录 {source}")
        if healthy + quarantined != source:
            problems.append(f"健康 {healthy} + 隔离 {quarantined} = {healthy + quarantined} ≠ 源记录 {source}")
        if shard_rows["healthy"] != healthy or shard_rows["quarantined"] != quarantined:
            problems.append("分片记账与作业记账不一致")
        if actual_quarantine != quarantined:
            problems.append(f"隔离表实际 {actual_quarantine} 行 ≠ 记账 {quarantined} 行")
        if problems:
            raise ReconcileError("发布核对失败：" + "；".join(problems))

        timestamp = now()
        conn.execute(
            "UPDATE runs SET publish_status='published', published_at=?, stage='已发布' WHERE run_id=?",
            (timestamp, run_id),
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('published_run_id', ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (run_id,),
        )
        conn.commit()
        detail["published_at"] = timestamp
        return detail


class ReconcileError(Exception):
    """发布核对不通过：保留旧版发布，不切换指针。"""


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
        "SELECT run_id, created_at, source, status, stage, rows, source_rows, healthy_rows,"
        " quarantine_rows, shard_count, confirmed_shards, publish_status, published_at,"
        " total_score, grade, error"
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


def get_published_run_id() -> str | None:
    return get_setting("published_run_id")


def get_published_run() -> dict | None:
    run_id = get_published_run_id()
    if not run_id:
        return None
    return get_run(run_id)


# ---------------------------------------------------------------- 隔离记录


def list_quarantine(run_id: str, limit: int = 200, offset: int = 0) -> list[dict]:
    conn = connect()
    rows = conn.execute(
        "SELECT id, run_id, shard_index, line_no, row_id, reason_code, reason, raw_text"
        " FROM quarantine_records WHERE run_id=? ORDER BY line_no, id LIMIT ? OFFSET ?",
        (run_id, limit, offset),
    ).fetchall()
    return [dict(r) for r in rows]


def count_quarantine(run_id: str) -> int:
    conn = connect()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM quarantine_records WHERE run_id=?", (run_id,),
    ).fetchone()
    return int(row["n"])


def quarantine_reason_summary(run_id: str) -> list[dict]:
    conn = connect()
    rows = conn.execute(
        "SELECT reason_code, COUNT(*) AS n FROM quarantine_records WHERE run_id=?"
        " GROUP BY reason_code ORDER BY n DESC, reason_code",
        (run_id,),
    ).fetchall()
    return [{"reason_code": r["reason_code"], "count": r["n"]} for r in rows]


def _row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    raw = data.pop("report_json", None)
    data["report"] = json.loads(raw) if raw else None
    intake = data.pop("intake_json", None)
    data["intake"] = json.loads(intake) if intake else None
    try:
        data["weight_overrides"] = json.loads(data.get("weight_overrides") or "{}")
    except json.JSONDecodeError:
        data["weight_overrides"] = {}
    return data


# ---------------------------------------------------------------- 设置项


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
