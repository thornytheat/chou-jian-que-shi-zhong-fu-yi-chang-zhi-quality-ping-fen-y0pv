"""分片作业端到端：分片确认、坏记录隔离、失败续跑不重复计数、核对闸门、原子发布。"""
from __future__ import annotations

import time

import pytest

from conftest import poll

from app import job, store

HEADER = "row_id,product_id,category,price,stock,weight,supplier,updated_at,rating\n"


@pytest.fixture(autouse=True)
def _clear_faults_between_tests():
    job.clear_faults()
    yield
    job.clear_faults()


def healthy_csv(rows: int = 120, *, bad: int = 0) -> str:
    lines = [HEADER]
    for i in range(1, rows + 1):
        lines.append(f"R{i:03d},P{i:03d},数码配件,{100 + i}.00,{i * 3},1.20,S01,2026-09-1{i % 9},4.5\n")
    for _ in range(bad):
        lines.append(",P900,数码配件,1.0,1,1.0,S1,2026-09-01,4.0\n")  # missing_row_id
    return "".join(lines)


def wait_status(client, run_id, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/runs/{run_id}/status").json()
        if data["status"] in ("done", "failed"):
            return data
        time.sleep(0.03)
    raise AssertionError(f"运行 {run_id} 超时")


def test_sharded_run_publishes_and_keeps_identity(client):
    res = client.post("/api/runs/text", json={"csv_text": healthy_csv(120, bad=3), "shard_size": 50})
    assert res.status_code == 200
    payload = res.json()
    assert payload["shard_count"] == 3
    assert payload["source_count"] == 123
    assert payload["healthy_count"] == 120
    assert payload["quarantine_count"] == 3

    st = wait_status(client, payload["run_id"])
    assert st["status"] == "done"
    assert st["publish_status"] == "published"
    assert st["confirmed_shards"] == 3
    assert st["source_rows"] == st["healthy_rows"] + st["quarantine_rows"]

    # 发布指针切到本次作业
    published = client.get("/api/published").json()
    assert published["run_id"] == payload["run_id"]

    shards = client.get(f"/api/runs/{payload['run_id']}/shards").json()["shards"]
    assert all(s["status"] == "confirmed" for s in shards)
    assert sum(s["total_records"] for s in shards) == 123
    assert sum(s["quarantine_records"] for s in shards) == 3

    q = client.get(f"/api/runs/{payload['run_id']}/quarantine").json()
    assert q["total"] == 3
    assert q["reason_summary"][0]["reason_code"] == "missing_row_id"
    assert all(item["reason"] for item in q["items"])  # 每条都有中文原因，不静默


def test_quarantine_csv_export(client):
    run_id = client.post("/api/runs/text",
                         json={"csv_text": healthy_csv(60, bad=2), "shard_size": 50}).json()["run_id"]
    wait_status(client, run_id)
    res = client.get(f"/api/runs/{run_id}/quarantine.csv")
    assert res.status_code == 200
    lines = res.text.lstrip("﻿").splitlines()
    assert lines[0] == "隔离序号,所属分片,源文件行号,row_id,原因码,原因说明,原始内容"
    assert len(lines) == 3  # 表头 + 2 条隔离


def test_failed_shard_resumes_without_double_counting(client):
    before_published = client.get("/api/published").json()["run_id"]

    # 故障在请求前排队，路由会在线程启动前绑定到新作业，无竞态
    job.schedule_fault(1)
    run_id = client.post("/api/runs/text",
                         json={"csv_text": healthy_csv(120), "shard_size": 50}).json()["run_id"]
    st = wait_status(client, run_id)
    assert st["status"] == "failed"
    assert st["shard_counters"]["confirmed"] == 1
    assert st["shard_counters"]["failed"] == 1
    # 失败期间发布指针不变，旧版保留
    assert client.get("/api/published").json()["run_id"] == before_published
    # 失败期间工单不可下发
    assert client.get(f"/api/runs/{run_id}/alerts.csv").status_code == 409

    # 续跑
    resume = client.post(f"/api/runs/{run_id}/resume")
    assert resume.status_code == 200
    st = wait_status(client, run_id)
    assert st["status"] == "done"
    assert st["publish_status"] == "published"
    assert st["healthy_rows"] == 120          # 关键：没有重复计数
    assert st["source_rows"] == 120

    shards = client.get(f"/api/runs/{run_id}/shards").json()["shards"]
    assert shards[1]["attempts"] == 2         # 失败片重试
    assert shards[0]["attempts"] == 1         # 已确认片没重跑
    assert shards[2]["attempts"] == 1
    assert all(s["status"] == "confirmed" for s in shards)
    # 续跑后发布指针切换
    assert client.get("/api/published").json()["run_id"] == run_id


def test_resume_409_when_not_failed(client):
    run_id = client.post("/api/runs/text",
                         json={"csv_text": healthy_csv(60), "shard_size": 50}).json()["run_id"]
    wait_status(client, run_id)
    res = client.post(f"/api/runs/{run_id}/resume")
    assert res.status_code == 409


def test_resume_404_for_unknown_run(client):
    assert client.post("/api/runs/ghost/resume").status_code == 404


def test_workorder_only_available_after_publish(client):
    # 构造一个失败作业：故障在请求前排队，注入到唯一分片
    job.schedule_fault(0)
    run_id = client.post("/api/runs/text",
                         json={"csv_text": healthy_csv(30), "shard_size": 50}).json()["run_id"]
    st = wait_status(client, run_id)
    assert st["status"] == "failed"
    res = client.get(f"/api/runs/{run_id}/alerts.csv")
    assert res.status_code == 409
    assert "尚未发布" in res.json()["detail"]

    # 旧版（seed）工单仍可下载
    old = client.get("/api/published").json()["run_id"]
    assert client.get(f"/api/runs/{old}/alerts.csv").status_code == 200


def test_shard_size_is_clamped(client):
    # 过小/过大/非数字都收敛到允许区间，不报错
    for size in (1, 99999, "abc"):
        res = client.post("/api/runs/text", json={"csv_text": healthy_csv(55), "shard_size": size})
        assert res.status_code == 200
        run_id = res.json()["run_id"]
        wait_status(client, run_id)
        assert res.json()["shard_size"] >= 50


def test_header_errors_still_return_400(client):
    # 表头缺列仍是 400（契约错误），与记录级隔离区分开
    broken = "row_id,product_id,category\nR001,P001,数码配件\n"
    res = client.post("/api/runs/text", json={"csv_text": broken})
    assert res.status_code == 400
    assert "缺少必需列" in res.json()["detail"]


def test_result_page_shows_job_view_while_running_and_result_after_publish(client):
    run_id = client.post("/api/runs/text",
                         json={"csv_text": healthy_csv(120, bad=2), "shard_size": 50}).json()["run_id"]
    wait_status(client, run_id)
    page = client.get(f"/result/{run_id}")
    assert page.status_code == 200
    assert "已发布版本" in page.text
    assert "隔离坏记录" in page.text
    # 隔离原因可在作业/结果链路看到
    q = client.get(f"/api/runs/{run_id}/quarantine").json()
    assert q["total"] == 2


def test_reconcile_blocks_publish_when_counts_drift(client):
    """核对闸门：记账与隔离表不符时拒绝发布，且不切换发布指针。"""
    import pytest

    run_id = client.post("/api/runs/text",
                         json={"csv_text": healthy_csv(60), "shard_size": 50}).json()["run_id"]
    wait_status(client, run_id)
    published_before = client.get("/api/published").json()["run_id"]

    # 直接在连接上把作业记账改坏（单线程测试无需进程锁），模拟记账漂移
    conn = store.connect()
    conn.execute("UPDATE runs SET publish_status='unpublished', quarantine_rows=quarantine_rows+1"
                 " WHERE run_id=?", (run_id,))
    conn.commit()
    try:
        with pytest.raises(store.ReconcileError) as exc:
            store.reconcile_and_publish(run_id)
        assert "核对失败" in str(exc.value)
        # 指针未被改动，线上仍是旧版
        assert client.get("/api/published").json()["run_id"] == published_before
    finally:
        conn.execute("UPDATE runs SET publish_status='published', quarantine_rows=quarantine_rows-1"
                     " WHERE run_id=?", (run_id,))
        conn.commit()


def test_reconcile_passes_for_consistent_job(client):
    run_id = client.post("/api/runs/text",
                         json={"csv_text": healthy_csv(60, bad=2), "shard_size": 50}).json()["run_id"]
    wait_status(client, run_id)
    # 已发布作业再次核对仍通过（幂等），且返回完整核对明细
    detail = store.reconcile_and_publish(run_id)
    assert detail["source"] == detail["healthy"] + detail["quarantined"]
    assert detail["confirmed_shards"] == detail["shard_count"]
    assert detail["actual_quarantine_rows"] == 2


def test_polling_contract_fields_present(client):
    run_id = client.post("/api/runs", json={"source": "sample", "shard_size": 500}).json()["run_id"]
    st = wait_status(client, run_id)
    for field in ("shards", "shard_counters", "publish_status", "published_run_id",
                  "source_rows", "healthy_rows", "quarantine_rows", "resumable"):
        assert field in st
    assert st["publish_status"] == "published"


def test_original_poll_helper_still_works(client):
    # 兼容旧测试夹具的 poll()
    data = poll(client, client.post("/api/runs", json={"source": "sample"}).json()["run_id"])
    assert data["status"] == "done"
    assert data["rows"] == 2000


def test_upload_gb18030_csv_with_bad_records(client):
    import io

    body = (HEADER + "R1,P1,数码配件,100.0,3,1.2,S1,2026-09-01,4.5\n"
            "\n,R2,数码配件,100.0,3,1.2,S1,2026-09-01,4.5\n").encode("gb18030")
    res = client.post("/api/runs/upload",
                      files={"file": ("gb.csv", io.BytesIO(body), "text/csv")})
    assert res.status_code == 200
    run_id = res.json()["run_id"]
    st = wait_status(client, run_id)
    assert st["status"] == "done" and st["publish_status"] == "published"
    assert (st["source_rows"], st["healthy_rows"], st["quarantine_rows"]) == (3, 1, 2)


def test_cell_level_quality_issues_still_become_alerts_not_quarantine(client):
    # 数字列写「面议」是健康行上的类型告警，不能被当成坏记录隔离
    text = (HEADER +
            "R1,P1,数码配件,面议,3,1.2,S1,2026-09-01,4.5\n"
            "R2,P2,数码配件,100.0,3,1.2,S1,2026-09-01,4.5\n")
    run_id = client.post("/api/runs/text", json={"csv_text": text}).json()["run_id"]
    st = wait_status(client, run_id)
    assert st["quarantine_rows"] == 0
    run = store.get_run(run_id)
    type_cells = run["report"]["validation"]["type"]["cells"]
    assert any(cell["row_id"] == "R1" and cell["column"] == "price" for cell in type_cells)


def test_mixed_bad_reasons_full_flow(client):
    text = HEADER + (
        "R1,P1,数码配件,100.0,3,1.2,S1,2026-09-01,4.5\n"
        "R2,P2,数码配件,100.0,3,1.2,S1,2026-09-01,4.5,EXTRA\n"   # field_count_mismatch
        "R3,P3,数码配件,100.0,3,1.2,S1,2026-09-01,4.5\n"
        "R1,P4,数码配件,100.0,3,1.2,S1,2026-09-01,4.5\n"        # duplicate_row_id
        ",P5,数码配件,100.0,3,1.2,S1,2026-09-01,4.5\n"          # missing_row_id
        "\n"                                                       # blank_line
    )
    run_id = client.post("/api/runs/text", json={"csv_text": text, "shard_size": 50}).json()["run_id"]
    st = wait_status(client, run_id)
    assert st["publish_status"] == "published"
    assert (st["source_rows"], st["healthy_rows"], st["quarantine_rows"]) == (6, 2, 4)
    summary = {item["reason_code"]: item["count"]
               for item in client.get(f"/api/runs/{run_id}/quarantine").json()["reason_summary"]}
    assert summary == {
        "field_count_mismatch": 1,
        "duplicate_row_id": 1,
        "missing_row_id": 1,
        "blank_line": 1,
    }


def test_quarantine_endpoint_pagination(client):
    text = HEADER + "".join(
        f",P{i:03d},数码配件,1.0,1,1.0,S1,2026-09-01,4.5\n" for i in range(5))
    run_id = client.post("/api/runs/text", json={"csv_text": text}).json()["run_id"]
    wait_status(client, run_id)
    page = client.get(f"/api/runs/{run_id}/quarantine?limit=2&offset=0").json()
    assert page["total"] == 5 and len(page["items"]) == 2
    page2 = client.get(f"/api/runs/{run_id}/quarantine?limit=2&offset=2").json()
    assert len(page2["items"]) == 2
    assert page["items"][0]["id"] != page2["items"][0]["id"]


def test_old_published_workorder_stays_available_during_failed_job(client):
    old = client.get("/api/published").json()["run_id"]
    job.schedule_fault(0)
    run_id = client.post("/api/runs/text",
                         json={"csv_text": healthy_csv(30), "shard_size": 50}).json()["run_id"]
    wait_status(client, run_id)
    # 新作业失败期间，旧版工单与结果页仍可正常访问
    assert client.get(f"/api/runs/{old}/alerts.csv").status_code == 200
    assert client.get(f"/result/{old}").status_code == 200
    assert client.get("/api/published").json()["run_id"] == old
