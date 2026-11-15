"""分片失败 → 续跑 → 不重复计数 → 旧版保留 的冒烟脚本（开发用）。"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "/home/node/.local/lib/python3.11/site-packages")
sys.path.insert(0, "/workspace")

from fastapi.testclient import TestClient  # noqa: E402

from app import job  # noqa: E402
from app.main import app  # noqa: E402


def wait(c: TestClient, run_id: str) -> dict:
    for _ in range(300):
        st = c.get(f"/api/runs/{run_id}/status").json()
        if st["status"] in ("done", "failed"):
            return st
        time.sleep(0.02)
    raise AssertionError("timeout")


def main() -> None:
    c = TestClient(app)
    c.get("/health")  # 触发 lifespan 与 seed
    before_pub = c.get("/api/published").json()["run_id"]
    print("published before:", before_pub)

    header = "row_id,product_id,category,price,stock,weight,supplier,updated_at,rating\n"
    lines = [header]
    for i in range(1, 121):  # 120 条健康记录 → shard_size=50 → 3 片
        lines.append(f"R{i:03d},P{i:03d},数码配件,{100 + i}.00,{i * 3},1.20,S01,2026-09-1{i % 9},4.5\n")
    # 2 条坏记录
    lines.append(",P901,数码配件,1.0,1,1.0,S1,2026-09-01,4.0\n")
    lines.append("R001,P902,数码配件,1.0,1,1.0,S1,2026-09-01,4.0\n")
    csv_text = "".join(lines)

    # 故障在请求前排队，路由在线程启动前绑定到新作业
    job.schedule_fault(1, RuntimeError, "模拟第 2 片落库中断")
    res = c.post("/api/runs/text", json={"csv_text": csv_text, "shard_size": 50})
    run_id = res.json()["run_id"]
    print("job started:", run_id, "shards:", res.json()["shard_count"],
          "source:", res.json()["source_count"])

    st = wait(c, run_id)
    print("after fault:", st["status"], "| error:", st["error"])
    print("shard counters:", st["shard_counters"])
    print("confirmed:", st["confirmed_shards"], "/", st["shard_count"])
    print("published pointer unchanged:", c.get("/api/published").json()["run_id"] == before_pub)

    # 失败期间工单不可下发
    wo = c.get(f"/api/runs/{run_id}/alerts.csv")
    print("workorder while failed:", wo.status_code)

    # 续跑
    rr = c.post(f"/api/runs/{run_id}/resume")
    print("resume:", rr.status_code, rr.json())
    st = wait(c, run_id)
    print("after resume:", st["status"], st["publish_status"])
    print("counts source/healthy/quar:", st["source_rows"], st["healthy_rows"], st["quarantine_rows"])

    # 关键：续跑后健康记录不重复（120），隔离仍是 2
    assert st["healthy_rows"] == 120, st["healthy_rows"]
    assert st["quarantine_rows"] == 2, st["quarantine_rows"]
    assert st["source_rows"] == 122
    shards = c.get(f"/api/runs/{run_id}/shards").json()["shards"]
    for s in shards:
        print("  shard", s["shard_index"], s["status"], "total/healthy/quar:",
              s["total_records"], s["healthy_records"], s["quarantine_records"],
              "attempts:", s["attempts"])
    # 第 2 片重试了 2 次；第 1、3 片只尝试 1 次
    assert shards[1]["attempts"] == 2
    assert shards[0]["attempts"] == 1 and shards[2]["attempts"] == 1

    # 发布指针已切换到新作业
    after_pub = c.get("/api/published").json()["run_id"]
    print("published after:", after_pub, "| switched:", after_pub == run_id)
    assert after_pub == run_id

    # 再点一次续跑 → 409（没有失败片）
    again = c.post(f"/api/runs/{run_id}/resume")
    print("resume again:", again.status_code)
    assert again.status_code == 409

    # 结果页与工单
    page = c.get(f"/result/{run_id}")
    print("result page:", page.status_code)
    wo = c.get(f"/api/runs/{run_id}/alerts.csv")
    print("workorder after publish:", wo.status_code, "| rows:",
          len([ln for ln in wo.text.splitlines() if ln.startswith("WO-")]))
    print("ALL ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
