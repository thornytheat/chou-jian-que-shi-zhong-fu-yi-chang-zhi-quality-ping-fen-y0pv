"""FastAPI 路由（薄）：参数解析 + 分片作业编排 + 渲染页面。

核心计算都在 missing / duplicates / outliers / score / alerts / validators 里，
路由只负责：取输入、起后台分片作业、读 SQLite 快照、套模板。
分片状态机与发布核对在 job.py / store.py，本文件不直接写分片表。
"""
from __future__ import annotations

import pathlib
import threading
import uuid

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import export, job, loader, pipeline, score, seed, settings, store
from .quarantine import REASON_LABELS

BASE_DIR = pathlib.Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["pct"] = lambda v, d=1: "—" if v is None else f"{float(v) * 100:.{d}f}%"
templates.env.filters["num"] = lambda v, d=2: "—" if v is None else f"{float(v):.{d}f}"
templates.env.filters["sev"] = lambda s: {"high": "err", "medium": "warn", "low": ""}.get(s, "")

@asynccontextmanager
async def lifespan(_: FastAPI):
    store.connect()
    ensure_seeded()
    yield


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

ALERT_PAGE_LIMIT = 80
QUARANTINE_PAGE_LIMIT = 100
_STATE: dict = {"rules": None, "seeded": False}
_LOCK = threading.Lock()


def rules() -> dict:
    if _STATE["rules"] is None:
        _STATE["rules"] = loader.load_rules()
    return _STATE["rules"]


def ensure_seeded() -> None:
    if _STATE["seeded"]:
        return
    with _LOCK:
        if _STATE["seeded"]:
            return
        seed.ensure_seed()
        _STATE["seeded"] = True


# ---------------------------------------------------------------- 页面

def column_specs(r: dict) -> list[dict]:
    """把 rules.json 的列定义投影成模板可直接渲染的只读结构（不含任何计算）。"""
    return [
        {
            "name": name,
            "label": spec.get("label", name),
            "type": spec.get("type"),
            "weight": spec.get("weight"),
            "max_missing_rate": spec.get("max_missing_rate"),
            "rules": [score.RULE_LABELS[x] for x in spec.get("rules", [])],
            "range": spec.get("range"),
        }
        for name, spec in r["columns"].items()
    ]


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    ensure_seeded()
    r = rules()
    history = store.list_runs(limit=5)
    published = store.get_published_run()
    latest = published or (history[0] if history else None)
    latest_report = (latest or {}).get("report") or {}
    # 首页统计：读当前已发布版本；没有任何发布时全部为 None
    stats = {
        "runs": store.count_runs(),
        "total": latest.get("total_score") if latest else None,
        "grade": latest.get("grade") if latest else None,
        "alerts": (latest_report.get("alerts") or {}).get("total"),
        "high": (latest_report.get("alerts") or {}).get("high"),
        "medium": (latest_report.get("alerts") or {}).get("medium"),
        "merged_cells": (latest_report.get("alerts") or {}).get("merged_cells"),
        "duplicate_groups": ((latest_report.get("duplicates") or {}).get("duplicate_key") or {}).get("group_count"),
        "row_groups": ((latest_report.get("duplicates") or {}).get("duplicate_row") or {}).get("group_count"),
        "outlier_cells": len((latest_report.get("outliers") or {}).get("cells") or []),
        "insufficient": len((latest_report.get("outliers") or {}).get("insufficient") or []),
    }
    return templates.TemplateResponse(request, "index.html", {
        "app_name": settings.APP_NAME,
        "page": "index",
        "rules": r,
        "columns": column_specs(r),
        "history": history,
        "latest": latest,
        "latest_report": latest_report,
        "stats": stats,
        "business_key": r.get("business_key"),
        "rule_weights": r.get("rule_weights", {}),
        "rule_labels": score.RULE_LABELS,
        "thresholds": r.get("grade_thresholds"),
        "outlier": r.get("outlier"),
        "min_group_sample": r.get("min_group_sample"),
        "sample_rows": 2000,
        "published": published,
    })


@app.get("/workbench", response_class=HTMLResponse)
def workbench(request: Request):
    ensure_seeded()
    r = rules()
    history = store.list_runs(limit=8)
    published = store.get_published_run()
    published_report = (published or {}).get("report") or {}
    return templates.TemplateResponse(request, "workbench.html", {
        "app_name": settings.APP_NAME,
        "page": "workbench",
        "columns": list(r["columns"].keys()),
        "column_specs": column_specs(r),
        "rule_weights": r.get("rule_weights", {}),
        "rule_labels": score.RULE_LABELS,
        "rules_version": r.get("version"),
        "business_key": r.get("business_key"),
        "group_by": r.get("group_by"),
        "outlier": r.get("outlier"),
        "min_group_sample": r.get("min_group_sample"),
        "thresholds": r.get("grade_thresholds"),
        "sample_rows": 2000,
        "history": history,
        "latest": history[0] if history else None,
        "stages": [label for _, label in pipeline.STAGES],
        "published": published,
        "published_report": published_report,
        "reason_labels": REASON_LABELS,
        "default_shard_size": settings.DEFAULT_SHARD_SIZE,
    })


def _job_view_context(request: Request, run: dict) -> dict:
    """运行中 / 失败 / 待发布作业的共用视图：分片进度 + 隔离记录 + 发布状态。"""
    shards = store.list_shards(run["run_id"])
    quarantine_items = store.list_quarantine(run["run_id"], limit=QUARANTINE_PAGE_LIMIT)
    quarantine_total = store.count_quarantine(run["run_id"])
    reason_summary = store.quarantine_reason_summary(run["run_id"])
    published = store.get_published_run()
    return {
        "app_name": settings.APP_NAME,
        "page": "result",
        "run": run,
        "shards": shards,
        "quarantine_items": quarantine_items,
        "quarantine_total": quarantine_total,
        "quarantine_shown": min(QUARANTINE_PAGE_LIMIT, quarantine_total),
        "reason_summary": reason_summary,
        "reason_labels": REASON_LABELS,
        "published": published,
    }


@app.get("/result/{run_id}", response_class=HTMLResponse)
def result(request: Request, run_id: str):
    ensure_seeded()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行记录 {run_id} 不存在，请回到工作台重新执行。")
    if run["status"] != "done" or run["publish_status"] != "published":
        # 运行中 / 失败 / 待发布：展示分片作业视图（含续跑入口），不暴露半成品结果
        return templates.TemplateResponse(request, "job.html", _job_view_context(request, run))
    report = run["report"]
    total_alerts = report["alerts"]["total"]
    limit = max(10, min(500, ALERT_PAGE_LIMIT))
    return templates.TemplateResponse(request, "result.html", {
        "app_name": settings.APP_NAME,
        "page": "result",
        "run": run,
        "report": report,
        "columns": report["dataset"]["column_names"],
        "score": report["score"],
        "alerts": report["alerts"]["items"][:limit],
        "alerts_shown": min(limit, total_alerts),
        "alerts_total": total_alerts,
        "alert_limit": limit,
        "insufficient": report["outliers"]["insufficient"],
        "rule_labels": score.RULE_LABELS,
        "severity_labels": {"high": "高", "medium": "中", "low": "低"},
        "reason_labels": REASON_LABELS,
    })


@app.get("/review", response_class=HTMLResponse)
def review(request: Request):
    ensure_seeded()
    r = rules()
    overrides = store.weight_overrides()
    columns, rule_weights = score.merged_weights(r, overrides)
    history = store.list_runs(limit=20)
    published = store.get_published_run()
    recent = published or (history[0] if history else None)
    return templates.TemplateResponse(request, "review.html", {
        "app_name": settings.APP_NAME,
        "page": "review",
        "rules": r,
        "history": history,
        "recent": recent,
        "published": published,
        "columns": [
            {"name": n, "label": s.get("label", n), "default": float(s.get("weight", 1.0)),
             "value": columns[n]}
            for n, s in r["columns"].items()
        ],
        "rule_weights": [
            {"name": k, "label": score.RULE_LABELS.get(k, k), "default": float(v),
             "value": rule_weights[k]}
            for k, v in rule_weights.items()
        ],
        "has_overrides": bool(overrides.get("columns") or overrides.get("rule_weights")),
        "override_count": len(overrides.get("columns") or {}) + len(overrides.get("rule_weights") or {}),
        "thresholds": r.get("grade_thresholds"),
    })


# ---------------------------------------------------------------- 分片作业 API


def _start_sharded_job(source: str, text: str, encoding: str, shard_size: int | None) -> dict:
    r = rules()
    overrides = store.weight_overrides()
    try:
        data = job.prepare_intake(text, r, source, shard_size or settings.DEFAULT_SHARD_SIZE)
    except loader.InputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    run_id = uuid.uuid4().hex[:12]
    data["encoding"] = encoding
    payload = job.intake_payload(data, encoding)
    store.create_run(run_id, source, overrides, intake=payload)
    store.init_shards(run_id, data["shard_size"], job.shard_summaries(data["shards"]))
    job.register_job_ctx(run_id, {"data": data, "rules": r, "weights": overrides})
    # 必须在线程 start 前绑定测试/演示故障，保证故障注入无竞态
    job.take_pending_faults(run_id)

    thread = threading.Thread(
        target=job.run_job, args=(run_id, data, r, overrides),
        name=f"shard-run-{run_id}", daemon=True,
    )
    thread.start()
    return {
        "run_id": run_id,
        "status": "running",
        "stages": [s[1] for s in pipeline.STAGES],
        "shard_count": len(data["shards"]),
        "shard_size": data["shard_size"],
        "source_count": data["source_count"],
        "healthy_count": data["healthy_count"],
        "quarantine_count": data["quarantine_count"],
    }


@app.post("/api/runs")
def create_run(payload: dict):
    ensure_seeded()
    payload = payload or {}
    source = payload.get("source", "sample")
    if source != "sample":
        raise HTTPException(status_code=400, detail="source 只支持 sample；上传请使用 /api/runs/upload。")
    r = rules()
    try:
        text, encoding = loader.sample_text(r)
    except loader.InputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _start_sharded_job(f"样例数据 {settings.SAMPLE_CSV.name}", text, encoding,
                              payload.get("shard_size"))


@app.post("/api/runs/upload")
async def create_run_upload(file: UploadFile = File(...), shard_size: int | None = None):
    ensure_seeded()
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传的文件为空，请选择含表头与数据行的 CSV。")
    if len(raw) > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="文件超过 8MB 上限，请分批抽检。")
    try:
        text, encoding = loader.decode_bytes(raw)
    except loader.InputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _start_sharded_job(f"上传文件 {file.filename or 'upload.csv'}", text, encoding, shard_size)


@app.post("/api/runs/text")
def create_run_text(payload: dict):
    """粘贴 CSV 文本执行（表头走严格契约校验，坏记录进隔离区而非报错）。"""
    ensure_seeded()
    text = (payload or {}).get("csv_text") or ""
    if not text.strip():
        raise HTTPException(status_code=400, detail="CSV 文本为空，请粘贴含表头的内容。")
    return _start_sharded_job("粘贴的 CSV 文本", text, "utf-8",
                              (payload or {}).get("shard_size"))


@app.post("/api/runs/{run_id}/resume")
def resume_run(run_id: str):
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行记录 {run_id} 不存在。")
    if run["status"] != "failed":
        raise HTTPException(status_code=409, detail="只有失败的作业可以续跑；该作业当前状态为 "
                            f"{run['status']}。")
    ctx = job.JOB_CTX.get(run_id)
    if ctx is None:
        raise HTTPException(
            status_code=409,
            detail="作业的内存上下文已随服务重启释放，已确认分片仍在库中；请重新上传 CSV 新建作业。",
        )
    started = job.resume_job(run_id, ctx["data"], ctx["rules"], ctx["weights"])
    if not started:
        raise HTTPException(status_code=409, detail="没有可续跑的失败分片。")
    return {"run_id": run_id, "status": "running", "resumed": True}


@app.get("/api/runs/{run_id}/status")
def run_status(run_id: str) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行记录 {run_id} 不存在。")
    stages = [{"name": name, "label": label} for name, label in pipeline.STAGES]
    report = run.get("report") or {}
    shards = store.list_shards(run_id)
    counters = {"confirmed": 0, "processing": 0, "pending": 0, "failed": 0}
    for shard in shards:
        counters[shard["status"]] = counters.get(shard["status"], 0) + 1
    published = store.get_published_run()
    return {
        "run_id": run_id,
        "status": run["status"],
        "stage": run["stage"],
        "stage_index": run["stage_index"],
        "stage_total": len(stages),
        "stages": stages,
        "rows": run["rows"],
        "source_rows": run["source_rows"],
        "healthy_rows": run["healthy_rows"],
        "quarantine_rows": run["quarantine_rows"],
        "total_score": run["total_score"],
        "grade": run["grade"],
        "error": run.get("error"),
        "alerts": (report.get("alerts") or {}).get("total"),
        "elapsed_ms": report.get("elapsed_ms"),
        "result_url": f"/result/{run_id}",
        "publish_status": run["publish_status"],
        "published_at": run["published_at"],
        "is_published_version": bool(published and published["run_id"] == run_id),
        "published_run_id": published["run_id"] if published else None,
        "shard_count": run["shard_count"],
        "shard_size": run["shard_size"],
        "confirmed_shards": run["confirmed_shards"],
        "shard_counters": counters,
        "shards": shards,
        "resumable": run["status"] == "failed" and run_id in job.JOB_CTX,
    }


@app.get("/api/runs/{run_id}/shards")
def run_shards(run_id: str) -> dict:
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail=f"运行记录 {run_id} 不存在。")
    return {"run_id": run_id, "shards": store.list_shards(run_id)}


@app.get("/api/runs/{run_id}/quarantine")
def run_quarantine(run_id: str, limit: int = 200, offset: int = 0) -> dict:
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail=f"运行记录 {run_id} 不存在。")
    limit = max(1, min(1000, limit))
    offset = max(0, offset)
    return {
        "run_id": run_id,
        "total": store.count_quarantine(run_id),
        "reason_summary": store.quarantine_reason_summary(run_id),
        "reason_labels": REASON_LABELS,
        "items": store.list_quarantine(run_id, limit=limit, offset=offset),
    }


@app.get("/api/runs/{run_id}/quarantine.csv", response_class=PlainTextResponse)
def quarantine_csv(run_id: str):
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行记录 {run_id} 不存在。")
    items = store.list_quarantine(run_id, limit=settings.MAX_ROWS)
    filename = f"qualiscan-quarantine-{run_id}.csv"
    return PlainTextResponse(
        export.quarantine_to_csv(items),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/runs/{run_id}/alerts.csv", response_class=PlainTextResponse)
def alerts_csv(run_id: str):
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行记录 {run_id} 不存在。")
    if run["publish_status"] != "published" or not run.get("report"):
        raise HTTPException(status_code=409,
                            detail="该次运行尚未发布，暂不能导出工单；失败期间请下载当前已发布旧版工单。")
    csv_text = export.alerts_to_csv(run["report"]["alerts"])
    filename = f"qualiscan-workorder-{run_id}.csv"
    return PlainTextResponse(
        csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/published")
def published_api() -> dict:
    run = store.get_published_run()
    if run is None:
        return {"published": False}
    report = run.get("report") or {}
    return {
        "published": True,
        "run_id": run["run_id"],
        "published_at": run["published_at"],
        "total_score": run["total_score"],
        "grade": run["grade"],
        "source_rows": run["source_rows"],
        "healthy_rows": run["healthy_rows"],
        "quarantine_rows": run["quarantine_rows"],
        "alerts": (report.get("alerts") or {}).get("total"),
        "workorder_url": f"/api/runs/{run['run_id']}/alerts.csv",
        "quarantine_url": f"/api/runs/{run['run_id']}/quarantine.csv",
        "result_url": f"/result/{run['run_id']}",
    }


@app.get("/api/rules/weights")
def get_weights() -> dict:
    r = rules()
    overrides = store.weight_overrides()
    columns, rule_weights = score.merged_weights(r, overrides)
    return {
        "columns": columns,
        "rule_weights": rule_weights,
        "defaults": {
            "columns": {n: float(s.get("weight", 1.0)) for n, s in r["columns"].items()},
            "rule_weights": {k: float(v) for k, v in r.get("rule_weights", {}).items()},
        },
        "overrides": overrides,
    }


@app.post("/api/rules/weights")
def set_weights(payload: dict) -> dict:
    r = rules()
    payload = payload or {}
    raw_columns = payload.get("columns") or {}
    raw_rules = payload.get("rule_weights") or {}
    columns: dict[str, float] = {}
    rule_weights: dict[str, float] = {}
    for name, value in raw_columns.items():
        if name not in r["columns"]:
            raise HTTPException(status_code=400, detail=f"未知列 {name}，无法设置权重。")
        columns[name] = _parse_weight(value, f"列 {name} 权重")
    for name, value in raw_rules.items():
        if name not in r.get("rule_weights", {}):
            raise HTTPException(status_code=400, detail=f"未知规则 {name}，无法设置权重。")
        rule_weights[name] = _parse_weight(value, f"规则 {name} 权重")
    saved = store.save_weight_overrides(columns, rule_weights)
    merged_columns, merged_rules = score.merged_weights(r, saved)
    return {"ok": True, "columns": merged_columns, "rule_weights": merged_rules}


def _parse_weight(value, label: str) -> float:
    try:
        weight = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{label} 必须是数字。") from exc
    if weight < 0 or weight > 10:
        raise HTTPException(status_code=400, detail=f"{label} 需在 0 ~ 10 之间。")
    return round(weight, 3)


@app.post("/api/rules/weights/reset")
def reset_weights() -> dict:
    store.save_weight_overrides({}, {})
    return {"ok": True}


@app.post("/api/runs/{run_id}/rescore")
def rescore(run_id: str) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行记录 {run_id} 不存在。")
    if run["publish_status"] != "published" or not run.get("report"):
        raise HTTPException(status_code=409, detail="该次运行尚未发布，无法重算。")
    r = rules()
    overrides = store.weight_overrides()
    report = run["report"]
    units = score.build_units(report["missing"], report["duplicates"],
                              report["outliers"], report["validation"], r)
    report["score"] = score.compute(units, r, overrides)
    store.update_report(run_id, report, overrides)
    return {
        "run_id": run_id,
        "total": report["score"]["total"],
        "grade": report["score"]["grade"],
        "weights": report["score"]["weights_used"],
    }


@app.get("/health")
def health() -> JSONResponse:
    ensure_seeded()
    published = store.get_published_run()
    return JSONResponse({
        "status": "ok",
        "app": settings.APP_SHORT,
        "runs": store.count_runs(),
        "rules_version": rules().get("version"),
        "data_dir": str(settings.DATA_DIR),
        "published_run_id": published["run_id"] if published else None,
    })


@app.get("/api/rules")
def rules_api() -> dict:
    return rules()


@app.get("/api/runs")
def runs_api(limit: int = 20) -> dict:
    return {"runs": store.list_runs(limit=max(1, min(100, limit)))}
