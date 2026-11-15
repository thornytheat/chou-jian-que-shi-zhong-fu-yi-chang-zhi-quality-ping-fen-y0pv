"""评分规则：权重归一、无样本满分、分级阈值与裁剪边界。"""
from __future__ import annotations

import copy

from app import score

RULES = {
    "grade_thresholds": {"A": 90, "B": 80, "C": 70},
    "rule_weights": {"missing": 1.0, "range": 1.0, "outlier": 2.0, "duplicate_row": 1.0},
    "columns": {
        "price": {"weight": 2.0, "rules": ["missing", "range"]},
        "stock": {"weight": 1.0, "rules": ["missing", "outlier"]},
        "row_id": {"weight": 0.5, "rules": ["missing"]},
    },
}


def unit(column: str, rule: str, samples: int, violations: int, note: str = "") -> dict:
    return {"column": column, "rule": rule, "samples": samples, "violations": violations, "note": note}


def test_weights_normalize_to_hundred():
    units = [
        unit("price", "missing", 100, 10),   # score 0.9, weight 2.0*1.0 = 2.0
        unit("stock", "missing", 100, 0),    # score 1.0, weight 1.0*1.0 = 1.0
    ]
    result = score.compute(units, RULES)
    assert result["weight_sum"] == 3.0
    # (0.9*2 + 1.0*1) / 3 = 0.9333...
    assert result["total"] == 93.33
    assert result["grade"] == "A"
    assert [i["score_pct"] for i in result["items"]] == [90.0, 100.0]


def test_rule_weight_multiplies_column_weight():
    units = [unit("stock", "outlier", 10, 5)]     # 1.0 * 2.0 = 2.0
    result = score.compute(units, RULES)
    item = result["items"][0]
    assert item["weight"] == 2.0
    assert item["score_pct"] == 50.0
    assert result["total"] == 50.0
    assert result["grade"] == "D"


def test_no_violation_is_full_marks():
    units = [unit("price", "missing", 1000, 0), unit("row_id", "missing", 1000, 0)]
    result = score.compute(units, RULES)
    assert result["total"] == 100.0
    assert result["grade"] == "A"
    assert all(i["score_pct"] == 100.0 for i in result["items"])


def test_grade_thresholds_boundaries():
    assert score.grade_for(90.0) == "A"
    assert score.grade_for(89.99) == "B"
    assert score.grade_for(80.0) == "B"
    assert score.grade_for(79.99) == "C"
    assert score.grade_for(70.0) == "C"
    assert score.grade_for(69.99) == "D"
    assert score.grade_for(100) == "A"
    # 阈值可被规则集覆盖
    assert score.grade_for(85.0, {"A": 95, "B": 85, "C": 75}) == "B"


def test_violation_rate_is_clamped():
    """违规数超过样本数（理论上不该出现）时分数裁剪到 0，不出现负分。"""
    assert score.clamp01(1 - 3.0) == 0.0
    assert score.clamp01(1 + 0.5) == 1.0
    result = score.compute([unit("price", "missing", 10, 30)], RULES)
    assert result["items"][0]["score_pct"] == 0.0
    assert result["total"] == 0.0


def test_empty_sample_scores_full_with_note():
    units = [unit("price", "missing", 0, 0), unit("stock", "missing", 100, 0)]
    result = score.compute(units, RULES)
    first = result["items"][0]
    assert first["samples"] == 0
    assert first["score_pct"] == 100.0
    assert "无样本" in first["note"]
    assert any("无样本" in n for n in result["notes"])
    # 无样本项仍计入权重，不改变归一化分母口径
    assert result["weight_sum"] == 3.0
    assert result["total"] == 100.0


def test_zero_total_weight_guarded():
    rules = copy.deepcopy(RULES)
    rules["columns"]["price"]["weight"] = 0.0
    result = score.compute([unit("price", "missing", 10, 10)], rules)
    assert result["weight_sum"] == 0.0
    assert result["total"] == 0.0


def test_weight_overrides_apply():
    overrides = {"columns": {"price": 4.0}, "rule_weights": {"outlier": 1.0}}
    columns, rule_weights = score.merged_weights(RULES, overrides)
    assert columns["price"] == 4.0
    assert rule_weights["outlier"] == 1.0
    assert columns["stock"] == 1.0              # 未被覆盖的保持默认
    result = score.compute([unit("price", "missing", 100, 50)], RULES, overrides)
    assert result["items"][0]["weight"] == 4.0
    assert result["total"] == 50.0


def test_build_units_from_modules_and_labels(dataset, rules):
    from app import duplicates, missing, outliers, validators
    from app.loader import missing_tokens

    tokens = missing_tokens(rules)
    miss = missing.analyze(dataset["rows"], dataset["columns"], rules)
    dup = duplicates.analyze(dataset["rows"], dataset["columns"], rules)
    out = outliers.analyze(dataset["rows"], dataset["columns"], rules)
    val = validators.analyze(dataset["rows"], dataset["columns"], rules, tokens)
    units = score.build_units(miss, dup, out, val, rules)
    result = score.compute(units, rules)
    # 每个启用规则都有评分单元，且整行重复用「（整行）」伪列承载
    rules_count = sum(len(spec.get("rules", [])) for spec in rules["columns"].values())
    assert len(units) == rules_count + 1
    assert any(u["column"] == score.ROW_UNIT for u in units)
    assert 0 < result["total"] <= 100
    assert result["grade"] in {"A", "B", "C", "D"}
    allowed_item = next(i for i in result["items"]
                        if i["column"] == "supplier" and i["rule"] == "missing")
    assert allowed_item["violations"] == 0          # 允许缺失列不扣分
    assert "允许缺失" in allowed_item["note"]
    rating_missing = next(i for i in result["items"]
                          if i["column"] == "rating" and i["rule"] == "missing")
    assert rating_missing["score_pct"] == 0.0       # 全空列 0 分
