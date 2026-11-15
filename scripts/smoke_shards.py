"""分片流程冒烟脚本（开发用，不在 pytest 收集范围）。

运行：QUALISCAN_DATA_DIR=<空目录> python3 -I scripts/smoke_shards.py
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "/home/node/.local/lib/python3.11/site-packages")
sys.path.insert(0, "/workspace")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def main() -> None:
    c = TestClient(app)

    health = c.get("/health").json()
    print("health:", health)
    pub = c.get("/api/published").json()
    print("seed published:", {k: pub.get(k) for k in
          ("run_id", "grade", "source_rows", "healthy_rows", "quarantine_rows")})

    header = "row_id,product_id,category,price,stock,weight,supplier,updated_at,rating\n"
    lines = [header]
    for i in range(1, 25):
        lines.append(f"R{i:03d},P{i:03d},数码配件,{100 + i}.00,{i * 3},1.20,S01,2026-09-1{i % 9},4.5\n")
    lines.append("\n")  # blank_line
    lines.append("R900,P900,数码配件,1.0,1,1.0,S1,2026-09-01,4.0,EXTRA\n")  # field_count_mismatch
    lines.append(",P901,数码配件,1.0,1,1.0,S1,2026-09-01,4.0\n")  # missing_row_id
    lines.append("R001,P902,数码配件,1.0,1,1.0,S1,2026-09-01,4.0\n")  # duplicate_row_id
    csv_text = "".join(lines)

    res = c.post("/api/runs/text", json={"csv_text": csv_text, "shard_size": 10})
    print("start:", res.status_code, res.json())
    run_id = res.json()["run_id"]

    for _ in range(200):
        st = c.get(f"/api/runs/{run_id}/status").json()
        if st["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    print("final:", st["status"], st["publish_status"],
          "shards:", st["confirmed_shards"], "/", st["shard_count"])
    print("counts source/healthy/quar:",
          st["source_rows"], st["healthy_rows"], st["quarantine_rows"])

    q = c.get(f"/api/runs/{run_id}/quarantine").json()
    print("quarantine total:", q["total"], "reasons:", q["reason_summary"])
    for item in q["items"]:
        print("  - line", item["line_no"], item["reason_code"], "|", item["reason"][:50])

    wo = c.get(f"/api/runs/{run_id}/alerts.csv")
    print("workorder http:", wo.status_code, wo.text[:60] if wo.status_code != 200 else "OK")
    qcsv = c.get(f"/api/runs/{run_id}/quarantine.csv")
    print("quarantine csv http:", qcsv.status_code,
          "| header:", qcsv.text.splitlines()[0].lstrip("﻿"))
    print("published pointer:", c.get("/api/published").json()["run_id"])
    page = c.get(f"/result/{run_id}")
    print("result page http:", page.status_code, "| has 已发布版本:", "已发布版本" in page.text)


if __name__ == "__main__":
    main()
