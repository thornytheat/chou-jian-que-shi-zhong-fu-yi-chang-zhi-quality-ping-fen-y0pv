"""重复检查：复合业务键重复（duplicate_key）与整行重复（duplicate_row）分开统计。

判定口径：
- 业务键 = rules["business_key"]（默认 product_id + category）；同一键出现多行 → 一组
  duplicate_key，组内额外行数 = 组大小 − 1。
- 整行重复 = 除 row_id 外所有列取值完全一致；同一签名多行 → 一组 duplicate_row。
- 去重口径：整行重复的行整体归入 duplicate_row，不再重复计入 duplicate_key，
  避免同一批行在两个规则下重复扣分（报告里用 excluded_from_key 注明条数）。
"""
from __future__ import annotations


def _signature(row: dict[str, str], columns: list[str]) -> tuple:
    return tuple(row.get(col, "") for col in columns)


def analyze(rows: list[dict[str, str]], columns: list[str], rules: dict) -> dict:
    key_columns = list(rules.get("business_key") or [])
    if not key_columns:
        key_columns = [c for c in columns if c != "row_id"][:2]
    compare_columns = [c for c in columns if c != "row_id"]

    # 1) 整行重复
    row_groups: dict[tuple, list[dict[str, str]]] = {}
    for row in rows:
        row_groups.setdefault(_signature(row, compare_columns), []).append(row)
    full_dups = [g for g in row_groups.values() if len(g) > 1]
    row_dup_ids = {row["row_id"] for group in full_dups for row in group}

    # 2) 复合键重复（排除整行重复行）
    key_groups: dict[tuple, list[dict[str, str]]] = {}
    for row in rows:
        if row["row_id"] in row_dup_ids:
            continue
        key_groups.setdefault(_signature(row, key_columns), []).append(row)
    key_dups = [g for g in key_groups.values() if len(g) > 1]

    return {
        "business_key": key_columns,
        "duplicate_row": _pack(row_dup_ids, full_dups, compare_columns, "整行"),
        "duplicate_key": _pack(set(), key_dups, key_columns, "业务键"),
        "excluded_from_key": len(row_dup_ids),
        "total_extra_rows": _extra(full_dups) + _extra(key_dups),
        "note": "整行重复已整体归入 duplicate_row，不再计入 duplicate_key",
    }


def _extra(groups: list[list[dict[str, str]]]) -> int:
    return sum(len(g) - 1 for g in groups)


def _pack(row_ids, groups, columns, label) -> dict:
    packed = []
    for group in groups:
        packed.append({
            "key": {col: group[0].get(col, "") for col in columns},
            "count": len(group),
            "extra": len(group) - 1,
            "row_ids": [row["row_id"] for row in group],
            "label": _label(group[0], columns, label),
        })
    packed.sort(key=lambda g: (-g["count"], g["row_ids"][0]))
    involved = set(row_ids) | {rid for g in packed for rid in g["row_ids"]}
    return {
        "columns": columns,
        "groups": packed,
        "group_count": len(packed),
        "row_count": len(involved),
        "extra_rows": _extra(groups),
        "row_ids": sorted(involved),
    }


def _label(row: dict[str, str], columns: list[str], kind: str) -> str:
    if len(columns) == 1:
        return f"{kind} {columns[0]}={row.get(columns[0], '')}"
    return kind + " " + " + ".join(f"{c}={row.get(c, '')}" for c in columns)
