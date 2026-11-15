"""评分卡：每列每规则 score = 1 − 违规率（裁剪到 [0,1]），按权重归一为百分制。

口径：
- 权重 = 列权重（rules.columns[列].weight）× 规则权重（rules.rule_weights[规则]）。
- 违规率 = 违规样本数 / 该规则的有效样本数；样本为 0 时按满分并注明「无样本」。
- 总分 = 100 × Σ(score × weight) / Σweight，四舍五入到 2 位。
- 分级：A ≥ 90 / B ≥ 80 / C ≥ 70 / D < 70（阈值来自 rules.grade_thresholds）。
- 整行重复属于行级规则，不计入某一列，用「（整行）」这一伪列承载，权重取规则权重。
"""
from __future__ import annotations

RULE_LABELS = {
    "missing": "缺失率",
    "type": "类型合法",
    "range": "取值范围",
    "outlier": "异常值",
    "duplicate_key": "复合键重复",
    "duplicate_row": "整行重复",
}

ROW_UNIT = "__row__"
ROW_UNIT_LABEL = "（整行）"


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def grade_for(total: float, thresholds: dict | None = None) -> str:
    th = thresholds or {"A": 90, "B": 80, "C": 70}
    if total >= th["A"]:
        return "A"
    if total >= th["B"]:
        return "B"
    if total >= th["C"]:
        return "C"
    return "D"


def merged_weights(rules: dict, overrides: dict | None = None) -> tuple[dict, dict]:
    """返回 (列权重表, 规则权重表)，overrides 形如 {"columns": {...}, "rule_weights": {...}}。"""
    columns = {name: float(spec.get("weight", 1.0)) for name, spec in rules["columns"].items()}
    rule_weights = {rule: float(w) for rule, w in rules.get("rule_weights", {}).items()}
    overrides = overrides or {}
    for name, weight in (overrides.get("columns") or {}).items():
        if name in columns:
            columns[name] = float(weight)
    for rule, weight in (overrides.get("rule_weights") or {}).items():
        if rule in rule_weights:
            rule_weights[rule] = float(weight)
    return columns, rule_weights


def build_units(missing: dict, duplicates: dict, outliers: dict, validation: dict,
                rules: dict) -> list[dict]:
    """把各模块的原始统计整理成「每列每规则」的评分单元。"""
    total_rows = missing["total_rows"] or 1
    units: list[dict] = []

    for column, spec in rules["columns"].items():
        active = spec.get("rules", [])
        miss_item = next((m for m in missing["columns"] if m["column"] == column), None)
        if "missing" in active and miss_item is not None:
            allowed = miss_item["status"] == "allowed"
            violations = 0 if allowed else miss_item["missing"]
            note = _missing_note(miss_item)
            units.append({
                "column": column,
                "rule": "missing",
                "samples": miss_item["total"],
                "violations": violations,
                "note": note,
            })
        if "type" in active:
            stat = validation["columns"].get(column, {})
            units.append({
                "column": column,
                "rule": "type",
                "samples": stat.get("checked", 0),
                "violations": stat.get("bad_type", 0),
                "note": "" if stat.get("checked") else "无样本（整列缺失），按满分",
            })
        if "range" in active:
            stat = validation["columns"].get(column, {})
            units.append({
                "column": column,
                "rule": "range",
                "samples": stat.get("checked", 0),
                "violations": stat.get("bad_range", 0),
                "note": "" if stat.get("checked") else "无样本（整列缺失），按满分",
            })
        if "outlier" in active:
            col = outliers["columns"].get(column, {})
            units.append({
                "column": column,
                "rule": "outlier",
                "samples": col.get("evaluated_sample", 0),
                "violations": len(col.get("cells", [])),
                "note": "" if col.get("evaluated_sample") else "无足够样本，按满分",
            })
        if "duplicate_key" in active:
            dup = duplicates["duplicate_key"]
            units.append({
                "column": column,
                "rule": "duplicate_key",
                "samples": total_rows - duplicates["excluded_from_key"],
                "violations": dup["extra_rows"],
                "note": _dup_note(dup, "复合业务键"),
            })

    dup_row = duplicates["duplicate_row"]
    units.append({
        "column": ROW_UNIT,
        "rule": "duplicate_row",
        "samples": total_rows,
        "violations": dup_row["extra_rows"],
        "note": _dup_note(dup_row, "整行"),
    })
    return units


def compute(units: list[dict], rules: dict, weights: dict | None = None,
            rule_weights: dict | None = None) -> dict:
    columns, rules_w = merged_weights(rules, weights)
    if rule_weights:
        rules_w.update({k: float(v) for k, v in rule_weights.items()})
    items = []
    weight_sum = 0.0
    weighted = 0.0
    notes: list[str] = []
    for unit in units:
        samples = int(unit["samples"])
        violations = int(unit["violations"])
        if samples <= 0:
            rate = 0.0
            score = 1.0
            note = unit.get("note") or "无样本，按满分"
            notes.append(f"{label_of(unit['column'])} · {RULE_LABELS[unit['rule']]} 无样本，按满分 100 计入")
        else:
            rate = violations / samples
            score = clamp01(1.0 - rate)
            note = unit.get("note") or ""
        col_weight = 1.0 if unit["column"] == ROW_UNIT else columns.get(unit["column"], 1.0)
        weight = col_weight * rules_w.get(unit["rule"], 1.0)
        items.append({
            "column": unit["column"],
            "column_label": label_of(unit["column"]),
            "rule": unit["rule"],
            "rule_label": RULE_LABELS[unit["rule"]],
            "samples": samples,
            "violations": violations,
            "rate": round(rate, 6),
            "score": round(score, 6),
            "score_pct": round(score * 100, 2),
            "column_weight": col_weight,
            "rule_weight": rules_w.get(unit["rule"], 1.0),
            "weight": round(weight, 4),
            "note": note,
        })
        weight_sum += weight
        weighted += score * weight

    total = 0.0 if weight_sum <= 0 else 100.0 * weighted / weight_sum
    total = round(total, 2)
    return {
        "items": items,
        "weight_sum": round(weight_sum, 4),
        "weighted_sum": round(weighted, 6),
        "total": total,
        "grade": grade_for(total, rules.get("grade_thresholds")),
        "thresholds": rules.get("grade_thresholds") or {"A": 90, "B": 80, "C": 70},
        "notes": notes,
        "weights_used": {"columns": columns, "rule_weights": rules_w},
    }


def label_of(column: str) -> str:
    return ROW_UNIT_LABEL if column == ROW_UNIT else column


def _missing_note(item: dict) -> str:
    if item["status"] == "all_empty":
        return "整列全空，按缺失违规计 0 分"
    if item["status"] == "violation":
        return f"缺失率 {item['rate'] * 100:.1f}% 超允许 {item['max_rate'] * 100:.1f}%"
    return f"规则允许缺失（{item['rate'] * 100:.1f}% ≤ {item['max_rate'] * 100:.1f}%），不扣分"


def _dup_note(dup: dict, label: str) -> str:
    if dup["group_count"] == 0:
        return f"未发现{label}重复"
    return f"{label}重复 {dup['group_count']} 组，涉及 {dup['row_count']} 行，额外 {dup['extra_rows']} 行"
