"""幂等 seed：首次启动用样例数据走完整分片流程并发布，供历史列表与复核页立刻有内容。

幂等口径：settings.seed_version 记录已完成的 seed 版本，重复启动不再插入新记录。
seed 与普通作业走同一条「分片确认 → 核对 → 统一发布」链路，保证演示数据本身就符合新口径。
旧版（v1）遗留的 seed 记录没有分片与发布状态，升级时整体删除后按新流程重建。
"""
from __future__ import annotations

from . import job, loader, settings, store

SEED_RUN_ID = "seed-sample"


def _purge_legacy_seed(existing: dict) -> bool:
    """v1 遗留 seed（无分片、未发布）无法续跑，删掉重建。返回是否执行了清理。"""
    if existing.get("publish_status") == "published":
        return False
    conn = store.connect()
    with store._LOCK:
        conn.execute("DELETE FROM quarantine_records WHERE run_id=?", (SEED_RUN_ID,))
        conn.execute("DELETE FROM job_shards WHERE run_id=?", (SEED_RUN_ID,))
        conn.execute("DELETE FROM runs WHERE run_id=?", (SEED_RUN_ID,))
        conn.execute("DELETE FROM settings WHERE key='published_run_id'")
        conn.commit()
    job.JOB_CTX.pop(SEED_RUN_ID, None)
    return True


def ensure_seed() -> dict:
    store.connect()
    rules = loader.load_rules()
    current = store.get_setting("seed_version")
    existing = store.get_run(SEED_RUN_ID)
    if existing is not None:
        if current == settings.SEED_VERSION and existing["publish_status"] == "published":
            return {"seeded": False, "run_id": SEED_RUN_ID, "rules": rules}
        _purge_legacy_seed(existing)
        existing = None

    if existing is None:
        text, encoding = loader.sample_text()
        data = job.prepare_intake(text, rules, f"样例数据 {settings.SAMPLE_CSV.name}",
                                  settings.DEFAULT_SHARD_SIZE)
        data["encoding"] = encoding
        payload = job.intake_payload(data, encoding)
        store.create_run(SEED_RUN_ID, data["source"], store.weight_overrides(), intake=payload)
        store.init_shards(SEED_RUN_ID, data["shard_size"], job.shard_summaries(data["shards"]))
        # seed 在启动流程内同步跑完（样例仅 2000 行），完成即发布
        job.register_job_ctx(SEED_RUN_ID, {"data": data, "rules": rules,
                                           "weights": store.weight_overrides()})
        job.run_job(SEED_RUN_ID, data, rules, store.weight_overrides())
        seeded = store.get_run(SEED_RUN_ID)
        if seeded is None or seeded["publish_status"] != "published":
            raise RuntimeError(f"seed 作业未成功发布：{(seeded or {}).get('error')}")
    store.set_setting("seed_version", settings.SEED_VERSION)
    return {"seeded": True, "run_id": SEED_RUN_ID, "rules": rules}
