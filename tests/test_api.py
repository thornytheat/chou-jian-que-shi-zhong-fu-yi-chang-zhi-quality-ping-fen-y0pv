"""接口测试：健康检查、成功剖析、错误输入 4xx、工单导出、权重重算、端到端主流程。"""
from __future__ import annotations

import io

from conftest import poll

HEADER = "row_id,product_id,category,price,stock,weight,supplier,updated_at,rating\n"


def valid_csv(rows: int = 12) -> str:
    lines = [HEADER]
    for i in range(1, rows + 1):
        lines.append(f"R{i:03d},P{i:03d},数码配件,{100 + i}.00,{i * 3},1.20,S01,2026-09-1{i % 9},4.5\n")
    return "".join(lines)


def test_health(client):
    data = client.get("/health").json()
    assert data["status"] == "ok"
    assert data["app"] == "qualiscan"
    assert data["rules_version"]
    assert data["runs"] >= 1


def test_pages_render(client):
    for path in ("/", "/workbench", "/review"):
        res = client.get(path)
        assert res.status_code == 200
        assert "qualiscan" in res.text or "抽检" in res.text
    home = client.get("/").text
    assert "缺失" in home and "异常值" in home and "整改工单" in home


def test_run_sample_and_result_page(client):
    res = client.post("/api/runs", json={"source": "sample"})
    assert res.status_code == 200
    run_id = res.json()["run_id"]
    status = poll(client, run_id)
    assert status["status"] == "done"
    assert status["rows"] == 2000
    assert status["grade"] in {"A", "B", "C", "D"}
    assert 0 < status["total_score"] <= 100

    page = client.get(f"/result/{run_id}")
    assert page.status_code == 200
    html = page.text
    assert "加权总分" in html and status["grade"] in html
    assert "每列每规则违规率" in html
    assert "样本不足" in html
    assert "合并" in html                        # 同格多规则合并的标记
    assert "R000031" in html                     # 越界+双法命中的单元格有据可查


def test_missing_column_csv_returns_400(client):
    broken = "row_id,product_id,category\nR001,P001,数码配件\n"
    res = client.post("/api/runs/text", json={"csv_text": broken})
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "缺少必需列" in detail
    assert "price" in detail


def test_upload_missing_column_returns_400(client):
    files = {"file": ("broken.csv", io.BytesIO(("row_id,product_id\nR1,P1\n").encode()), "text/csv")}
    res = client.post("/api/runs/upload", files=files)
    assert res.status_code == 400
    assert "缺少必需列" in res.json()["detail"]


def test_empty_and_header_only_csv_return_400(client):
    empty = client.post("/api/runs/text", json={"csv_text": "   "})
    assert empty.status_code == 400
    header_only = client.post("/api/runs/text", json={"csv_text": HEADER})
    assert header_only.status_code == 400
    assert "没有数据行" in header_only.json()["detail"]


def test_unknown_run_returns_404(client):
    assert client.get("/result/not-exists").status_code == 404
    assert client.get("/api/runs/not-exists/status").status_code == 404
    assert client.get("/api/runs/not-exists/alerts.csv").status_code == 404


def test_bad_source_returns_400(client):
    res = client.post("/api/runs", json={"source": "ftp"})
    assert res.status_code == 400
    assert "source" in res.json()["detail"]


def test_workorder_csv_export(client):
    run_id = client.post("/api/runs", json={"source": "sample"}).json()["run_id"]
    poll(client, run_id)
    res = client.get(f"/api/runs/{run_id}/alerts.csv")
    assert res.status_code == 200
    assert "text/csv" in res.headers["content-type"]
    assert "attachment" in res.headers["content-disposition"]
    text = res.text
    assert text.lstrip("\ufeff").startswith("工单号,row_id,column,rules,severity")
    assert "WO-0001" in text
    # 同一单元格只出现一次：按 (row_id, column) 统计行数
    body = [line for line in text.splitlines() if line.startswith("WO-")]
    assert len(body) >= 10
    assert len(body) == len({line.split(",")[1] + "|" + line.split(",")[2] for line in body})


def test_weight_update_and_rescore(client):
    run_id = client.post("/api/runs", json={"source": "sample"}).json()["run_id"]
    before = poll(client, run_id)
    res = client.post("/api/rules/weights",
                      json={"columns": {"rating": 6.0}, "rule_weights": {}})
    assert res.status_code == 200
    assert res.json()["columns"]["rating"] == 6.0

    rescored = client.post(f"/api/runs/{run_id}/rescore")
    assert rescored.status_code == 200
    after = rescored.json()
    assert after["total"] < before["total_score"]        # 加大全空列权重 → 分数下降
    assert after["grade"] in {"A", "B", "C", "D"}
    assert client.get("/api/rules/weights").json()["columns"]["rating"] == 6.0

    reset = client.post("/api/rules/weights/reset")
    assert reset.status_code == 200
    assert client.get("/api/rules/weights").json()["columns"]["rating"] == 1.2


def test_weight_validation_400(client):
    bad = client.post("/api/rules/weights", json={"columns": {"nope": 1}})
    assert bad.status_code == 400
    assert "未知列" in bad.json()["detail"]
    too_big = client.post("/api/rules/weights", json={"columns": {"price": 99}})
    assert too_big.status_code == 400
    not_number = client.post("/api/rules/weights", json={"columns": {"price": "abc"}})
    assert not_number.status_code == 400


def test_end_to_end_upload_flow(client):
    """完整主流程：上传合法 CSV → 轮询 → 结果页 → 工单 CSV。"""
    files = {"file": ("quality.csv", io.BytesIO(valid_csv().encode("utf-8")), "text/csv")}
    res = client.post("/api/runs/upload", files=files)
    assert res.status_code == 200
    run_id = res.json()["run_id"]
    status = poll(client, run_id)
    assert status["status"] == "done", status
    assert status["rows"] == 12
    assert status["grade"] in {"A", "B", "C", "D"}

    page = client.get(f"/result/{run_id}")
    assert page.status_code == 200
    csv_res = client.get(f"/api/runs/{run_id}/alerts.csv")
    assert csv_res.status_code == 200
