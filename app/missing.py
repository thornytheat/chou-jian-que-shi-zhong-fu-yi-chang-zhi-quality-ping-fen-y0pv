"""缺失检查：按列算缺失率，与规则允许的 max_missing_rate 比较。

判定口径：
- 缺失值 = 空串或 missing_tokens 中的占位符（NA/NULL/- 等），大小写不敏感。
- status = "all_empty"（整列全空，最严重）/ "violation"（缺失率 > 允许值）/ "allowed"。
- allowed 的列在评分中按 0 违规处理（规则允许缺失，不扣分），并在报告里注明。
"""
from __future__ import annotations


def is_missing(value: str, tokens: tuple[str, ...]) -> bool:
    return ("" if value is None else str(value)).strip().lower() in tokens


def analyze(rows: list[dict[str, str]], columns: list[str], rules: dict) -> dict:
    tokens = tuple(str(t).strip().lower() for t in rules.get("missing_tokens", []))
    total = len(rows)
    items = []
    violations = []
    allowed = []
    all_empty = []
    total_missing = 0
    for column in columns:
        spec = rules["columns"].get(column, {})
        max_rate = float(spec.get("max_missing_rate", 0.0))
        cells = [row["row_id"] for row in rows if is_missing(row.get(column, ""), tokens)]
        miss = len(cells)
        total_missing += miss
        rate = (miss / total) if total else 0.0
        if miss == total and total > 0:
            status = "all_empty"
        elif rate > max_rate:
            status = "violation"
        else:
            status = "allowed"
        item = {
            "column": column,
            "total": total,
            "missing": miss,
            "rate": round(rate, 6),
            "max_rate": max_rate,
            "status": status,
            "allowed": status == "allowed",
            "all_empty": status == "all_empty",
            "in_scope": "missing" in spec.get("rules", []),
            "sample_row_ids": cells[:20],
            "note": _note(status, rate, max_rate, miss, total),
        }
        items.append(item)
        if status == "violation":
            violations.append(item)
        elif status == "all_empty":
            all_empty.append(item)
        else:
            allowed.append(item)
    return {
        "total_rows": total,
        "columns": items,
        "total_missing_cells": total_missing,
        "violations": violations,
        "allowed": allowed,
        "all_empty": all_empty,
        "tokens": list(tokens),
    }


def _note(status: str, rate: float, max_rate: float, miss: int, total: int) -> str:
    if status == "all_empty":
        return f"整列 {total} 行全部为空，属最严重的缺失违规"
    if status == "violation":
        return f"缺失 {miss}/{total} = {rate * 100:.1f}%，超过允许 {max_rate * 100:.1f}%"
    return f"缺失 {miss}/{total} = {rate * 100:.1f}%，未超允许 {max_rate * 100:.1f}%（规则允许缺失）"

