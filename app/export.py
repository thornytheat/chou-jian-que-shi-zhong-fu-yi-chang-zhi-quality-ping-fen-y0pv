"""工单导出：把去重后的告警渲染成可直接下发的 CSV（Excel 友好，带 BOM）。"""
from __future__ import annotations

import csv
import io

HEADER = ["工单号", "row_id", "column", "rules", "severity", "当前值", "问题说明", "整改建议"]


def alerts_to_csv(alerts: dict) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(HEADER)
    for index, item in enumerate(alerts["items"], start=1):
        writer.writerow([
            f"WO-{index:04d}",
            item["row_id"],
            item["column"],
            item["rules_text"],
            item["severity"],
            item["value"],
            item["reason"],
            item["suggestion"],
        ])
    return "\ufeff" + buffer.getvalue()
