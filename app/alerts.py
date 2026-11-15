"""告警与工单：把各规则命中的单元格合并成去重后的告警列表。

去重口径：同一 (row_id, column) 无论被几条规则命中，只保留一条告警，
rules[] 保存全部命中规则，severity 取其中最严重的一条。
行级（整行重复）用 column="（整行）"；整列（全空列）用 row_id="—" 的列级告警，
避免 2000 行同类问题刷屏，具体行数在各规则统计里另有计数。
"""
from __future__ import annotations

SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}
SEVERITY_LABEL = {"low": "低", "medium": "中", "high": "高"}
RULE_LABEL = {
    "missing": "缺失",
    "type": "类型错误",
    "range": "越界",
    "outlier_iqr": "异常值(IQR)",
    "outlier_mad": "异常值(稳健z)",
    "duplicate_key": "复合键重复",
    "duplicate_row": "整行重复",
}
SUGGESTION = {
    "missing": "补齐该字段：向上游补采或按规则判定为不适用后标注",
    "type": "按契约类型重新采集并回写，附原始值凭证",
    "range": "核对量纲/单位与采价口径，越界值退回上游重报",
    "outlier_iqr": "人工复核该值是否录入错误，抽样比价后确认或修正",
    "outlier_mad": "人工复核该值是否录入错误，抽样比价后确认或修正",
    "duplicate_key": "保留最新一条主数据，其余作废并回溯下游引用",
    "duplicate_row": "删除完全重复的行，保留首次入库记录",
}
ROW_UNIT = "（整行）"
COLUMN_SCOPE = "column"
MISSING_SAMPLE_LIMIT = 20
RULE_ORDER = ["duplicate_row", "duplicate_key", "range", "type", "outlier_mad", "outlier_iqr", "missing"]


def _rank(severity: str) -> int:
    return SEVERITY_RANK.get(severity, 0)


def _empty() -> dict:
    return {}


def build(missing: dict, duplicates: dict, outliers: dict, validation: dict) -> dict:
    entries: list[dict] = []
    tokens_note = "空值或占位符"

    # 缺失：违规列按行号取前 20 个样例单元格，超出部分汇总成一条列级告警
    for item in missing["columns"]:
        if item["status"] == "violation":
            for row_id in item["sample_row_ids"][:MISSING_SAMPLE_LIMIT]:
                entries.append({
                    "row_id": row_id,
                    "column": item["column"],
                    "rule": "missing",
                    "severity": "medium",
                    "value": "",
                    "reason": f"该列为空（{tokens_note}），列缺失率 {item['rate'] * 100:.1f}% 超过允许 {item['max_rate'] * 100:.1f}%",
                    "scope": "cell",
                })
            if item["missing"] > MISSING_SAMPLE_LIMIT:
                entries.append({
                    "row_id": "—",
                    "column": item["column"],
                    "rule": "missing",
                    "severity": "medium",
                    "value": "",
                    "reason": (
                        f"本列共 {item['missing']} 行缺失（{item['rate'] * 100:.1f}%），"
                        f"超过允许 {item['max_rate'] * 100:.1f}%；"
                        f"此处仅逐行列出前 {MISSING_SAMPLE_LIMIT} 行，其余按列汇总"
                    ),
                    "scope": COLUMN_SCOPE,
                    "rows_affected": item["missing"],
                })
        elif item["status"] == "all_empty":
            entries.append({
                "row_id": "—",
                "column": item["column"],
                "rule": "missing",
                "severity": "high",
                "value": "",
                "reason": f"整列 {item['total']} 行全部为空（缺失率 100%），无法参与任何计算",
                "scope": COLUMN_SCOPE,
                "rows_affected": item["missing"],
            })

    # 重复：复合键 → 键列单元格告警；整行 → 行级告警
    key_dup = duplicates["duplicate_key"]
    for group in key_dup["groups"]:
        for row_id in group["row_ids"]:
            for column in key_dup["columns"]:
                entries.append({
                    "row_id": row_id,
                    "column": column,
                    "rule": "duplicate_key",
                    "severity": "medium",
                    "value": group["key"].get(column, ""),
                    "reason": f"业务键（{' + '.join(key_dup['columns'])}）重复：{group['label']}，本组 {group['count']} 行",
                    "scope": "cell",
                    "group_rows": group["row_ids"],
                })
    row_dup = duplicates["duplicate_row"]
    for group in row_dup["groups"]:
        for row_id in group["row_ids"]:
            entries.append({
                "row_id": row_id,
                "column": ROW_UNIT,
                "rule": "duplicate_row",
                "severity": "high",
                "value": "",
                "reason": f"除 row_id 外 8 列与同组其他行完全一致，本组 {group['count']} 行：{'、'.join(group['row_ids'])}",
                "scope": "row",
                "group_rows": group["row_ids"],
            })

    # 范围越界
    for cell in validation["range"]["cells"]:
        entries.append({
            "row_id": cell["row_id"],
            "column": cell["column"],
            "rule": "range",
            "severity": "medium",
            "value": cell["raw"],
            "reason": cell["reason"],
            "scope": "cell",
        })

    # 类型错误
    for cell in validation["type"]["cells"]:
        entries.append({
            "row_id": cell["row_id"],
            "column": cell["column"],
            "rule": "type",
            "severity": "medium",
            "value": cell["value"],
            "reason": f"类型错误：{cell['reason']}，期望 {cell['expected']}",
            "scope": "cell",
        })

    # 异常值：IQR / 稳健z 分开记规则，靠合并去重落到同一单元格
    for cell in outliers["cells"]:
        rules_hit = []
        if cell["iqr_hit"]:
            rules_hit.append("outlier_iqr")
        if cell["mad_hit"]:
            rules_hit.append("outlier_mad")
        for rule in rules_hit:
            entries.append({
                "row_id": cell["row_id"],
                "column": cell["column"],
                "rule": rule,
                "severity": cell["severity"],
                "value": f"{cell['value']:g}",
                "reason": (
                    f"{cell['group']} 组内中位数 {cell['median']:g}，"
                    f"IQR 区间 [{cell['bounds'][0]:g}, {cell['bounds'][1]:g}]，"
                    f"稳健 z={cell['robust_z'] if cell['robust_z'] is not None else 'MAD=0 不可判'}"
                ),
                "scope": "cell",
            })

    return finalize(entries)


def finalize(entries: list[dict]) -> dict:
    merged: dict[tuple[str, str], dict] = {}
    for entry in entries:
        key = (entry["row_id"], entry["column"])
        current = merged.get(key)
        if current is None:
            merged[key] = {
                "row_id": entry["row_id"],
                "column": entry["column"],
                "rules": [entry["rule"]],
                "severities": {entry["rule"]: entry["severity"]},
                "severity": entry["severity"],
                "value": entry["value"],
                "reasons": [entry["reason"]],
                "scope": entry.get("scope", "cell"),
                "group_rows": entry.get("group_rows"),
                "rows_affected": entry.get("rows_affected"),
            }
            continue
        if entry["rule"] not in current["rules"]:
            current["rules"].append(entry["rule"])
        current["severities"][entry["rule"]] = entry["severity"]
        if _rank(entry["severity"]) > _rank(current["severity"]):
            current["severity"] = entry["severity"]
        if entry["reason"] not in current["reasons"]:
            current["reasons"].append(entry["reason"])
        if entry.get("group_rows") and not current.get("group_rows"):
            current["group_rows"] = entry["group_rows"]
        if entry.get("rows_affected"):
            current["rows_affected"] = entry["rows_affected"]

    items = []
    for item in merged.values():
        item["rules"] = sorted(item["rules"], key=_rule_sort_key)
        item["rule_labels"] = [RULE_LABEL[r] for r in item["rules"]]
        item["rules_text"] = "、".join(item["rules"])
        item["merged_from"] = len(item["rules"])
        item["value"] = item["value"] or "—"
        item["reason"] = "；".join(item["reasons"])
        item["suggestion"] = _suggestion(item["rules"])
        item["severity_label"] = SEVERITY_LABEL.get(item["severity"], item["severity"])
        items.append(item)

    items.sort(key=lambda a: (-_rank(a["severity"]), a["column"], a["row_id"]))
    by_rule: dict[str, int] = {}
    for item in items:
        for rule in item["rules"]:
            by_rule[rule] = by_rule.get(rule, 0) + 1
    return {
        "items": items,
        "total": len(items),
        "high": sum(1 for i in items if i["severity"] == "high"),
        "medium": sum(1 for i in items if i["severity"] == "medium"),
        "merged_cells": sum(1 for i in items if i["merged_from"] > 1),
        "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
        "by_severity": {
            "high": sum(1 for i in items if i["severity"] == "high"),
            "medium": sum(1 for i in items if i["severity"] == "medium"),
            "low": sum(1 for i in items if i["severity"] == "low"),
        },
    }


def _rule_sort_key(rule: str) -> int:
    return RULE_ORDER.index(rule) if rule in RULE_ORDER else len(RULE_ORDER)


def _suggestion(rules: list[str]) -> str:
    parts = []
    for rule in rules:
        text = SUGGESTION.get(rule)
        if text and text not in parts:
            parts.append(text)
    return "；".join(parts) if parts else "人工复核该单元格"
