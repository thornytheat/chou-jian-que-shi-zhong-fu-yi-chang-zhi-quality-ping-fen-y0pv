"""工单导出：把去重后的告警渲染成可直接下发的 CSV（Excel 友好，带 BOM）。"""
from __future__ import annotations

import csv
import io

from .quarantine import reason_label

HEADER = ["工单号", "row_id", "column", "rules", "severity", "当前值", "问题说明", "整改建议"]

QUARANTINE_HEADER = ["隔离序号", "所属分片", "源文件行号", "row_id", "原因码", "原因说明", "原始内容"]


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


def quarantine_to_csv(items: list[dict]) -> str:
    """隔离记录 CSV：每条坏记录一行，原因码与中文说明并列，便于退回上游修正。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(QUARANTINE_HEADER)
    for seq, item in enumerate(items, start=1):
        writer.writerow([
            seq,
            item["shard_index"] + 1,
            item["line_no"],
            item.get("row_id") or "—",
            item["reason_code"],
            item.get("reason") or reason_label(item["reason_code"]),
            item.get("raw_text", ""),
        ])
    return "\ufeff" + buffer.getvalue()
