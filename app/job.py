"""分片作业编排：隔离 → 分片确认 → 续跑 → 核对 → 统一发布。

状态机：
  pending → processing → confirmed（健康行 + 隔离记录在同一事务落库，失败回滚不留半片）
                       ↘ failed（记录原因；续跑时退回 pending 重做，confirmed 片不重跑、不重复计数）
  全片 confirmed 且 source = healthy + quarantined → 跑 7 阶段剖析 → reconcile_and_publish 原子发布。

失败注入：模块级 _FAULT_HOOK 仅供测试/演示（如某片处理到一半抛错），
key 为 (run_id, shard_index)，一次性触发后自动清除，模拟真实的单片失败与续跑。
"""
from __future__ import annotations

import threading

from . import loader, pipeline, quarantine, settings, store

# run_id -> 线程锁，避免同一作业被重复启动（接口并发点两次）
_JOB_LOCKS: dict[str, threading.Lock] = {}
_REGISTRY_LOCK = threading.Lock()

# 进程内作业上下文：分片源记录只在内存持有，确认一片就把该片健康行落库；
# 服务重启后已确认片与隔离记录仍在 SQLite，未确认片需重新上传新建作业（接口 409 提示）。
# 仅保留最近若干个作业的上下文供续跑，避免长进程内存无限增长。
JOB_CTX: dict[str, dict] = {}
_JOB_CTX_LIMIT = 32


def register_job_ctx(run_id: str, ctx: dict) -> None:
    JOB_CTX[run_id] = ctx
    if len(JOB_CTX) <= _JOB_CTX_LIMIT:
        return
    # 按插入顺序淘汰最旧的作业（dict 保序）；正在运行的作业不在淘汰候选里
    for old_id in list(JOB_CTX.keys()):
        if len(JOB_CTX) <= _JOB_CTX_LIMIT:
            break
        run = store.get_run(old_id)
        if run and run["status"] == "running":
            continue
        JOB_CTX.pop(old_id, None)

# 测试/演示用故障注入（确定性，无竞态）：
#   测试在线程启动前调 schedule_fault 排队；路由创建作业时（线程 start 之前）
#   用 take_pending_faults 把故障挂到新作业上，worker 处理到该片必然触发一次。
_FAULT_HOOK: dict[tuple[str, int], tuple[type[Exception], str]] = {}
_PENDING_FAULTS: list[tuple[int, type[Exception], str]] = []


def job_lock(run_id: str) -> threading.Lock:
    with _REGISTRY_LOCK:
        lock = _JOB_LOCKS.get(run_id)
        if lock is None:
            lock = threading.Lock()
            _JOB_LOCKS[run_id] = lock
        return lock


def schedule_fault(shard_index: int,
                   exc: type[Exception] = RuntimeError,
                   message: str = "注入的分片处理失败") -> None:
    """排队一个分片故障，作用于下一个创建的作业（必须在发起请求前调用）。"""
    _PENDING_FAULTS.append((shard_index, exc, message))


def take_pending_faults(run_id: str) -> None:
    """路由在 worker 线程启动前调用：把排队故障绑定到本作业。"""
    while _PENDING_FAULTS:
        shard_index, exc, message = _PENDING_FAULTS.pop(0)
        _FAULT_HOOK[(run_id, shard_index)] = (exc, message)


def clear_faults() -> None:
    _FAULT_HOOK.clear()
    _PENDING_FAULTS.clear()


def _maybe_fault(run_id: str, shard_index: int) -> None:
    fault = _FAULT_HOOK.pop((run_id, shard_index), None)
    if fault is not None:
        exc_type, message = fault
        raise exc_type(message)


# ---------------------------------------------------------------- 作业准备


def prepare_intake(text: str, rules: dict, source: str, shard_size: int) -> dict:
    """解析 + 隔离 + 切片：返回作业创建所需的全部结构化数据（不落库）。"""
    shard_size = _clamp_shard_size(shard_size)
    data = loader.intake_text(text, rules, source)
    intake: quarantine.IntakeResult = data["intake"]
    shards = _build_shards(intake, shard_size)
    data["shard_size"] = shard_size
    data["shards"] = shards
    data["columns"] = intake.columns
    return data


def _clamp_shard_size(shard_size: int) -> int:
    try:
        size = int(shard_size)
    except (TypeError, ValueError):
        return settings.DEFAULT_SHARD_SIZE
    return max(settings.MIN_SHARD_SIZE, min(settings.MAX_SHARD_SIZE, size))


def _build_shards(intake: quarantine.IntakeResult, shard_size: int) -> list[dict]:
    """按源记录顺序切片；每片记账 total = 健康 + 隔离，保证核对恒等式可逐片验证。"""
    shards: list[dict] = []
    records = intake.records
    for index, start in enumerate(range(0, len(records), shard_size)):
        chunk = records[start:start + shard_size]
        healthy = [r for r in chunk if r.is_healthy]
        bad = [r for r in chunk if not r.is_healthy]
        shards.append({
            "shard_index": index,
            "total_records": len(chunk),
            "healthy_records": len(healthy),
            "quarantine_records": len(bad),
            "records": chunk,
        })
    if not shards:
        raise loader.InputError("CSV 只有表头、没有数据行。")
    return shards


def shard_summaries(shards: list[dict]) -> list[dict]:
    return [
        {k: s[k] for k in ("shard_index", "total_records", "healthy_records", "quarantine_records")}
        for s in shards
    ]


def intake_payload(data: dict, encoding: str = "utf-8") -> dict:
    """落库用的轻量 intake 快照（不放全部行，行数据随确认片持久化）。"""
    return {
        "columns": data["columns"],
        "source": data["source"],
        "encoding": encoding,
        "source_count": data["source_count"],
        "healthy_count": data["healthy_count"],
        "quarantine_count": data["quarantine_count"],
        "shard_size": data["shard_size"],
        "reason_summary": _reason_summary(data["intake"]),
    }


def _reason_summary(intake: quarantine.IntakeResult) -> list[dict]:
    counts: dict[str, int] = {}
    for record in intake.bad_records:
        counts[record.bad_reason] = counts.get(record.bad_reason, 0) + 1
    return [{"reason_code": code, "count": n} for code, n in sorted(counts.items())]


def _quarantine_items(records: list[quarantine.ParsedRecord]) -> list[dict]:
    items = []
    for record in records:
        if record.is_healthy:
            continue
        items.append({
            "line_no": record.line_no,
            "row_id": (record.row or {}).get("row_id") if record.row else None,
            "reason_code": record.bad_reason,
            "reason": record.bad_detail or quarantine.reason_label(record.bad_reason),
            "raw_text": _restore_line(record.raw_fields),
        })
    return items


def _restore_line(fields: list[str]) -> str:
    import csv
    import io

    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="").writerow(fields or [])
    return buffer.getvalue()


# ---------------------------------------------------------------- 作业执行


def run_job(run_id: str, data: dict, rules: dict, weights: dict) -> bool:
    """后台线程入口：逐片确认 → 汇总 → 剖析 → 核对 → 发布。任何一步失败都保留旧发布。

    返回 False 表示同一作业已有线程在跑（本次未启动）；True 表示由本次调用执行完毕。
    """
    if not job_lock(run_id).acquire(blocking=False):
        return False  # 同一作业已有线程在跑
    try:
        shards = data["shards"]
        for shard in shards:
            index = shard["shard_index"]
            current = _shard_status(run_id, index)
            if current == "confirmed":
                continue  # 续跑：已确认片直接跳过，绝不重复计数
            try:
                store.mark_shard_processing(run_id, index)
                store.set_progress(
                    run_id, index,
                    f"分片导入 {index + 1}/{len(shards)}（{shard['total_records']} 条，"
                    f"含 {shard['quarantine_records']} 条隔离）",
                )
                _maybe_fault(run_id, index)
                _process_shard(run_id, shard)
            except Exception as exc:  # noqa: BLE001 - 单片失败要落库给页面看
                store.fail_shard(run_id, index, f"{type(exc).__name__}: {exc}")
                return True

        _finalize(run_id, data, rules, weights)
        return True
    finally:
        job_lock(run_id).release()


def _shard_status(run_id: str, index: int) -> str | None:
    for shard in store.list_shards(run_id):
        if shard["shard_index"] == index:
            return shard["status"]
    return None


def _process_shard(run_id: str, shard: dict) -> None:
    """确认一片：健康行走 rows_json，坏行走 quarantine_records，同一事务提交。"""
    records = shard["records"]
    healthy_rows = [r.row for r in records if r.is_healthy]
    quarantine_rows = _quarantine_items(records)
    # 再次校验恒等式，防止记账漂移
    if shard["total_records"] != len(healthy_rows) + len(quarantine_rows):
        raise RuntimeError("分片记账与实际记录数不一致，拒绝确认该片")
    store.confirm_shard(run_id, shard["shard_index"], healthy_rows, quarantine_rows)


def resume_job(run_id: str, data: dict, rules: dict, weights: dict) -> bool:
    """续跑入口：失败片退回 pending 后重跑；已确认片不重跑。返回是否启动了续跑。

    注意：续跑线程必须在锁释放后再启动，否则新线程里的 run_job 抢不到作业锁会静默退出。
    """
    run = store.get_run(run_id)
    if run is None:
        raise JobNotFound(run_id)
    if run["status"] != "failed":
        return False
    lock = job_lock(run_id)
    if not lock.acquire(blocking=False):
        return False
    try:
        reset = store.reset_failed_shards(run_id)
        if reset == 0:
            return False
    finally:
        lock.release()
    thread = threading.Thread(
        target=run_job, args=(run_id, data, rules, weights),
        name=f"resume-{run_id}", daemon=True,
    )
    thread.start()
    return True


class JobNotFound(Exception):
    pass


def _finalize(run_id: str, data: dict, rules: dict, weights: dict) -> None:
    """全片确认后：汇总健康行 → 7 阶段剖析 → 报告落库 → 核对 → 原子发布。"""
    store.set_progress(run_id, 90, "全部分片已确认，汇总健康记录")
    healthy_rows = store.load_confirmed_rows(run_id)
    columns = data["columns"]

    # 汇总后再做一次跨片核对（源 = 健康 + 隔离），不符直接失败、不发布
    run = store.get_run(run_id)
    source = run["source_rows"]
    quarantined = store.count_quarantine(run_id)
    if len(healthy_rows) != run["healthy_rows"] or quarantined != run["quarantine_rows"]:
        store.fail_run(run_id, "汇总核对失败：确认片落库的健康/隔离计数与作业记账不一致")
        return
    if len(healthy_rows) + quarantined != source:
        store.fail_run(
            run_id,
            f"汇总核对失败：健康 {len(healthy_rows)} + 隔离 {quarantined} "
            f"= {len(healthy_rows) + quarantined} ≠ 源记录 {source}",
        )
        return

    store.set_progress(run_id, 92, "执行 7 阶段质量剖析")

    def progress(index: int, label: str) -> None:
        store.set_progress(run_id, 92 + index, label)

    report = pipeline.run(healthy_rows, columns, rules, weights, progress)
    report["run_id"] = run_id
    report["dataset"]["source"] = data["source"]
    report["dataset"]["encoding"] = data.get("encoding", "utf-8")
    report["dataset"]["source_rows"] = source
    report["dataset"]["healthy_rows"] = len(healthy_rows)
    report["dataset"]["quarantine_rows"] = quarantined
    report["dataset"]["shard_size"] = data["shard_size"]
    report["dataset"]["shard_count"] = len(data["shards"])
    report["quarantine"] = {
        "total": quarantined,
        "reason_summary": store.quarantine_reason_summary(run_id),
    }
    store.finish_run(run_id, report)

    store.set_progress(run_id, 98, "发布核对（源 = 健康 + 隔离）")
    try:
        detail = store.reconcile_and_publish(run_id)
    except store.ReconcileError as exc:
        store.fail_run(run_id, str(exc))
        return
    store.set_progress(run_id, 99, f"已发布（{detail['published_at']}）")
