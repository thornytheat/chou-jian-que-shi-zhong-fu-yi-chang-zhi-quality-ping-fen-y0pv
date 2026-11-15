"""幂等 seed：首次启动用样例数据跑一次剖析，供历史列表与复核页立刻有内容。

幂等口径：settings.seed_version 记录已完成的 seed 版本，重复启动不再插入新记录。
"""
from __future__ import annotations

from . import loader, pipeline, settings, store

SEED_RUN_ID = "seed-sample"


def ensure_seed() -> dict:
    store.connect()
    rules = loader.load_rules()
    current = store.get_setting("seed_version")
    if current == settings.SEED_VERSION and store.get_run(SEED_RUN_ID):
        return {"seeded": False, "run_id": SEED_RUN_ID, "rules": rules}
    if store.get_run(SEED_RUN_ID) is None:
        data = loader.load_sample(rules)
        report = pipeline.run(data["rows"], data["columns"], rules,
                              weights=store.weight_overrides())
        report["dataset"]["source"] = data["source"]
        report["dataset"]["encoding"] = data.get("encoding", "utf-8")
        report["seeded"] = True
        store.create_run(SEED_RUN_ID, data["source"], store.weight_overrides())
        store.finish_run(SEED_RUN_ID, report)
    store.set_setting("seed_version", settings.SEED_VERSION)
    return {"seeded": True, "run_id": SEED_RUN_ID, "rules": rules}
