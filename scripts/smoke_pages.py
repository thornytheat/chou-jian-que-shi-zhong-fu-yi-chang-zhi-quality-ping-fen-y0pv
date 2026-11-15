"""页面渲染冒烟：作业页（运行中/失败/续跑）、工作台、复核页、结果页。"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "/home/node/.local/lib/python3.11/site-packages")
sys.path.insert(0, "/workspace")

from fastapi.testclient import TestClient  # noqa: E402

from app import job  # noqa: E402
from app.main import app  # noqa: E402

HEADER = "row_id,product_id,category,price,stock,weight,supplier,updated_at,rating\n"


def main() -> None:
    c = TestClient(app)
    c.get("/health")

    lines = [HEADER]
    for i in range(1, 121):
        lines.append(f"R{i:03d},P{i:03d},数码配件,{100 + i}.00,{i * 3},1.20,S01,2026-09-1{i % 9},4.5\n")
    # 第 1 片内的坏记录（失败前就会随该片确认进入隔离表）
    lines.insert(2, ",P800,数码配件,1.0,1,1.0,S1,2026-09-01,4.0\n")  # missing_row_id
    lines.append("\n")  # 空白行（在第 3 片）
    lines.append("R001,P902,数码配件,1.0,1,1.0,S1,2026-09-01,4.0\n")  # 重复 row_id（在第 3 片）
    job.schedule_fault(1)  # 故障在请求前排队，绑定到下一个作业
    run_id = c.post("/api/runs/text",
                    json={"csv_text": "".join(lines), "shard_size": 50}).json()["run_id"]

    for _ in range(200):
        st = c.get(f"/api/runs/{run_id}/status").json()
        if st["status"] in ("done", "failed"):
            break
        time.sleep(0.02)
    assert st["status"] == "failed"

    # 失败态作业页（坏记录在未确认片中，隔离表只含已确认片，页面仍展示隔离面板与空态）
    page = c.get(f"/result/{run_id}")
    assert page.status_code == 200
    html = page.text
    for needle in ("分片作业", "从已确认处续跑", "隔离记录", "发布状态与工单去向",
                   "线上旧版", "核对相符", "未发布（保留旧版）", "missing_row_id"):
        assert needle in html, f"缺少 {needle}"
    # 失败时只有第 1 片已确认，其隔离记录已可见；失败片里的坏记录尚未入表
    assert "本次导入没有坏记录" not in html
    print("失败态作业页 OK：分片进度/已确认片隔离记录/发布状态/工单去向/续跑按钮均在")

    # 工作台与复核页
    wb = c.get("/workbench").text
    for needle in ("分片进度与发布状态", "当前已发布版本与工单去向", "分片大小", "隔离 CSV"):
        assert needle in wb, f"工作台缺少 {needle}"
    print("工作台 OK")

    rv = c.get("/review").text
    for needle in ("源/健康/隔离", "已发布", "隔离记录 CSV"):
        assert needle in rv, f"复核页缺少 {needle}"
    print("复核页 OK")

    # 续跑后结果页：坏记录随第 3 片确认进入隔离表
    c.post(f"/api/runs/{run_id}/resume")
    for _ in range(200):
        st = c.get(f"/api/runs/{run_id}/status").json()
        if st["status"] in ("done", "failed"):
            break
        time.sleep(0.02)
    assert st["status"] == "done" and st["publish_status"] == "published"
    assert st["quarantine_rows"] == 3

    result = c.get(f"/result/{run_id}").text
    for needle in ("已发布版本", "隔离坏记录", "条坏记录已隔离", "下载隔离记录 CSV"):
        assert needle in result, f"结果页缺少 {needle}"
    print("发布后结果页 OK")

    q = c.get(f"/api/runs/{run_id}/quarantine").json()
    codes = {item["reason_code"] for item in q["items"]}
    assert codes == {"blank_line", "duplicate_row_id", "missing_row_id"}, codes
    print("隔离原因码 OK：", codes)

    # 首页
    home = c.get("/").text
    assert "源/健康/隔离" in home and "已发布" in home
    print("首页 OK")
    print("页面渲染全部通过")


if __name__ == "__main__":
    main()
