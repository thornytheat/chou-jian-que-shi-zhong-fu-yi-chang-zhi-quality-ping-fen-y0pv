"""类型与取值范围校验：按列规则判断单元格是否合法，输出可追溯的单元格证据。

- type: number 要求可解析为数字；date 要求 YYYY-MM-DD 且日期真实存在；string 不校验。
- range: 数值列的 [min, max] 闭区间；非数字单元格不计入 range（已由 type 规则命中）。
- 缺失单元格不参与这两条规则（缺失由缺失规则单独统计）。
"""
from __future__ import annotations

import datetime

from .loader import to_float


def parse_date(text: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(text.strip())
    except (ValueError, AttributeError):
        return None


def analyze(rows: list[dict[str, str]], columns: list[str], rules: dict,
            tokens: tuple[str, ...]) -> dict:
    type_cells: list[dict] = []
    range_cells: list[dict] = []
    column_stats = {}

    for column in columns:
        spec = rules["columns"].get(column, {})
        active = spec.get("rules", [])
        kind = spec.get("type", "string")
        bounds = spec.get("range") or {}
        low = bounds.get("min")
        high = bounds.get("max")
        checked = 0
        bad_type = 0
        bad_range = 0
        for row in rows:
            raw = row.get(column, "")
            if str(raw).strip().lower() in tokens:
                continue
            checked += 1
            value = None
            if kind == "number":
                value = to_float(raw)
                if value is None:
                    if "type" in active:
                        bad_type += 1
                        type_cells.append({
                            "row_id": row["row_id"],
                            "column": column,
                            "value": raw,
                            "expected": "数字",
                            "reason": f"「{raw}」不是合法数字",
                        })
                    continue
            elif kind == "date":
                if parse_date(raw) is None:
                    if "type" in active:
                        bad_type += 1
                        type_cells.append({
                            "row_id": row["row_id"],
                            "column": column,
                            "value": raw,
                            "expected": "YYYY-MM-DD",
                            "reason": f"「{raw}」不是合法日期（含不存在的日）",
                        })
                    continue
            if "range" in active and value is not None:
                if (low is not None and value < float(low)) or (high is not None and value > float(high)):
                    bad_range += 1
                    range_cells.append({
                        "row_id": row["row_id"],
                        "column": column,
                        "value": value,
                        "raw": raw,
                        "range": [low, high],
                        "reason": f"取值 {_fmt(value)} 超出允许范围 [{_fmt(low)}, {_fmt(high)}]",
                    })
        column_stats[column] = {
            "column": column,
            "type": kind,
            "checked": checked,
            "bad_type": bad_type,
            "bad_range": bad_range,
            "range": [low, high] if "range" in active else None,
        }

    return {
        "type": {"cells": type_cells, "count": len(type_cells)},
        "range": {"cells": range_cells, "count": len(range_cells)},
        "columns": column_stats,
    }


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return str(value)
