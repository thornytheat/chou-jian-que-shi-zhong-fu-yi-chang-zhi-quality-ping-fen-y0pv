"""FastAPI 路由（薄）：参数解析 + 调用剖析流水线 + 渲染页面。

核心计算都在 missing / duplicates / outliers / score / alerts / validators 里，
路由只负责：取输入、起后台线程、读 SQLite 快照、套模板。
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

from . import export, loader, pipeline, score, seed, settings, store

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
    latest = history[0] if history else None
    latest_report = (store.get_run(latest["run_id"]) or {}).get("report") if latest else None
    latest_report = latest_report or {}
    # 首页统计：只读最近一次运行的报告快照，没有运行记录时全部为 None
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
    })


@app.get("/workbench", response_class=HTMLResponse)
def workbench(request: Request):
    ensure_seeded()
    r = rules()
    history = store.list_runs(limit=5)
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
    })


@app.get("/result/{run_id}", response_class=HTMLResponse)
def result(request: Request, run_id: str, limit: int = ALERT_PAGE_LIMIT):
    ensure_seeded()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行记录 {run_id} 不存在，请回到工作台重新执行。")
    if run["status"] == "running":
        return templates.TemplateResponse(request, "running.html", {
            "app_name": settings.APP_NAME,
            "page": "result",
            "run": run,
        })
    if run["status"] == "failed":
        raise HTTPException(status_code=409, detail=f"该次运行失败：{run.get('error') or '未知错误'}")
    report = run["report"]
    total_alerts = report["alerts"]["total"]
    limit = max(10, min(500, limit))
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
    })


@app.get("/review", response_class=HTMLResponse)
def review(request: Request):
    ensure_seeded()
    r = rules()
    overrides = store.weight_overrides()
    columns, rule_weights = score.merged_weights(r, overrides)
    history = store.list_runs(limit=20)
    recent = history[0] if history else None
    return templates.TemplateResponse(request, "review.html", {
        "app_name": settings.APP_NAME,
        "page": "review",
        "rules": r,
        "history": history,
        "recent": recent,
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


# ---------------------------------------------------------------- API


def _start_run(source: str, data: dict) -> dict:
    r = rules()
    overrides = store.weight_overrides()
    run_id = uuid.uuid4().hex[:12]
    store.create_run(run_id, source, overrides)
    rows, columns = data["rows"], data["columns"]

    def worker() -> None:
        try:
            def progress(index: int, label: str) -> None:
                store.set_progress(run_id, index, label)

            report = pipeline.run(rows, columns, r, overrides, progress)
            report["run_id"] = run_id
            report["dataset"]["source"] = source
            report["dataset"]["encoding"] = data.get("encoding", "utf-8")
            store.finish_run(run_id, report)
        except Exception as exc:  # noqa: BLE001 - 记录失败原因给页面看
            store.fail_run(run_id, f"{type(exc).__name__}: {exc}")

    threading.Thread(target=worker, name=f"run-{run_id}", daemon=True).start()
    return {"run_id": run_id, "status": "running", "stages": [s[1] for s in pipeline.STAGES]}


@app.post("/api/runs")
def create_run(payload: dict) -> dict:
    ensure_seeded()
    source = (payload or {}).get("source", "sample")
    if source != "sample":
        raise HTTPException(status_code=400, detail="source 只支持 sample；上传请使用 /api/runs/upload。")
    r = rules()
    try:
        data = loader.load_sample(r)
    except loader.InputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _start_run(data["source"], data)


@app.post("/api/runs/upload")
async def create_run_upload(file: UploadFile = File(...)) -> dict:
    ensure_seeded()
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传的文件为空，请选择含表头与数据行的 CSV。")
    if len(raw) > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="文件超过 8MB 上限，请分批抽检。")
    r = rules()
    try:
        text, encoding = loader.decode_bytes(raw)
        data = loader.load_csv_text(text, r, f"上传文件 {file.filename or 'upload.csv'}")
    except loader.InputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    data["encoding"] = encoding
    return _start_run(data["source"], data)


@app.post("/api/runs/text")
def create_run_text(payload: dict) -> dict:
    """粘贴 CSV 文本执行（同样走严格表头校验）。"""
    ensure_seeded()
    text = (payload or {}).get("csv_text") or ""
    if not text.strip():
        raise HTTPException(status_code=400, detail="CSV 文本为空，请粘贴含表头的内容。")
    r = rules()
    try:
        data = loader.load_csv_text(text, r, "粘贴的 CSV 文本")
    except loader.InputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _start_run(data["source"], data)


@app.get("/api/runs/{run_id}/status")
def run_status(run_id: str) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行记录 {run_id} 不存在。")
    stages = [{"name": name, "label": label} for name, label in pipeline.STAGES]
    report = run.get("report") or {}
    return {
        "run_id": run_id,
        "status": run["status"],
        "stage": run["stage"],
        "stage_index": run["stage_index"],
        "stage_total": len(stages),
        "stages": stages,
        "rows": run["rows"],
        "total_score": run["total_score"],
        "grade": run["grade"],
        "error": run.get("error"),
        "alerts": (report.get("alerts") or {}).get("total"),
        "elapsed_ms": report.get("elapsed_ms"),
        "result_url": f"/result/{run_id}",
    }


@app.get("/api/runs/{run_id}/alerts.csv", response_class=PlainTextResponse)
def alerts_csv(run_id: str):
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行记录 {run_id} 不存在。")
    if run["status"] != "done" or not run.get("report"):
        raise HTTPException(status_code=409, detail="该次运行尚未完成，暂不能导出工单。")
    csv_text = export.alerts_to_csv(run["report"]["alerts"])
    filename = f"qualiscan-workorder-{run_id}.csv"
    return PlainTextResponse(
        csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
    if run["status"] != "done" or not run.get("report"):
        raise HTTPException(status_code=409, detail="该次运行尚未完成，无法重算。")
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
    return JSONResponse({
        "status": "ok",
        "app": settings.APP_SHORT,
        "runs": store.count_runs(),
        "rules_version": rules().get("version"),
        "data_dir": str(settings.DATA_DIR),
    })


@app.get("/api/rules")
def rules_api() -> dict:
    return rules()


@app.get("/api/runs")
def runs_api(limit: int = 20) -> dict:
    return {"runs": store.list_runs(limit=max(1, min(100, limit)))}
