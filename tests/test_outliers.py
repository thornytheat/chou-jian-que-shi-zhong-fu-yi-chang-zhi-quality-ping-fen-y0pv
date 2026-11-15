"""异常值规则：分位数、IQR 边界、MAD、双法分级、样本不足、MAD=0 边界。"""
from __future__ import annotations

import pytest

from app import outliers

PARAMS = {
    "iqr_k": 1.5,
    "mad_k": 3.5,
    "dual_hit_severity": "high",
    "single_hit_severity": "medium",
    "min_sample": 8,
}


def rules_for(min_sample: int = 8, column: str = "price") -> dict:
    return {
        "group_by": "category",
        "min_group_sample": min_sample,
        "outlier": {"iqr_k": 1.5, "mad_k": 3.5,
                    "dual_hit_severity": "high", "single_hit_severity": "medium"},
        "columns": {column: {"rules": ["outlier"]}},
    }


def make_rows(values, category="A", column="price") -> list[dict]:
    return [
        {"row_id": f"R{i:04d}", "category": category, column: str(value)}
        for i, value in enumerate(values)
    ]


def test_quantile_linear_interpolation():
    assert outliers.quantile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
    assert outliers.quantile([1.0, 2.0, 3.0, 4.0], 0.25) == 1.75
    assert outliers.quantile([5.0], 0.9) == 5.0
    with pytest.raises(ValueError):
        outliers.quantile([], 0.5)


def test_iqr_bounds_known_values():
    bounds = outliers.iqr_bounds([float(v) for v in range(1, 10)])
    assert bounds["q1"] == 3.0
    assert bounds["q3"] == 7.0
    assert bounds["iqr"] == 4.0
    assert bounds["low"] == -3.0
    assert bounds["high"] == 13.0


def test_mad_and_robust_z():
    values = [1.0, 2.0, 3.0, 4.0, 100.0]
    assert outliers.median(values) == 3.0
    assert outliers.mad(values) == 1.0
    z = outliers.robust_z(100.0, 3.0, 1.0)
    assert z is not None
    assert round(z, 4) == round(97.0 / 1.4826, 4)
    # MAD=0 时不可判定，返回 None（不产生误报）
    assert outliers.robust_z(5.0, 1.0, 0.0) is None


def test_both_methods_hit_is_high():
    values = [10.0] * 20 + [110.0] * 20 + [10000.0]
    verdict = outliers.classify(10000.0, values, PARAMS)
    assert verdict["iqr_hit"] is True
    assert verdict["mad_hit"] is True
    assert verdict["severity"] == "high"


def test_single_iqr_hit_is_medium():
    """分布很宽（IQR 100、MAD 50）时，刚过 IQR 上界的值 z 只有约 3.24 → 只算 medium。"""
    values = [10.0] * 20 + [110.0] * 20
    verdict = outliers.classify(300.0, values, PARAMS)
    assert verdict["iqr_hit"] is True
    assert verdict["mad_hit"] is False
    assert verdict["severity"] == "medium"
    assert verdict["z"] is not None and verdict["z"] < 3.5


def test_insufficient_sample_is_not_judged():
    rows = make_rows([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 9999.0])  # 只有 7 个样本 < 8
    result = outliers.analyze(rows, ["price"], rules_for(min_sample=8))
    assert result["cells"] == []
    assert len(result["insufficient"]) == 1
    assert result["insufficient"][0]["sample"] == 7
    assert result["insufficient"][0]["min_sample"] == 8
    assert result["columns"]["price"]["groups"][0]["status"] == "insufficient_sample"


def test_median_zero_mad_group_reports_no_outlier():
    """组内全部取值相同（MAD=0）时不判异常，并标注 mad_zero。"""
    rows = make_rows([0.5] * 12)
    result = outliers.analyze(rows, ["price"], rules_for())
    assert result["cells"] == []
    group = result["columns"]["price"]["groups"][0]
    assert group["mad"] == 0.0
    assert group["mad_zero"] is True
    assert group["status"] == "evaluated"


def test_outliers_are_group_scoped():
    """同样的极大值，在两个品类里表现不同：小品类样本不足，大品类双法命中。"""
    rows = make_rows([10.0] * 20 + [110.0] * 20 + [5000.0], category="大品类")
    rows += make_rows([10.0, 11.0, 12.0], category="小品类")
    result = outliers.analyze(rows, ["price"], rules_for())
    assert len(result["cells"]) == 1
    cell = result["cells"][0]
    assert cell["group"] == "大品类"
    assert cell["severity"] == "high"
    assert set(cell["methods"]) == {"IQR", "稳健z"}
    assert result["insufficient"][0]["group"] == "小品类"


def test_sample_data_outliers_and_boundaries(dataset, rules):
    result = outliers.analyze(dataset["rows"], dataset["columns"], rules)
    assert result["high"] >= 2                      # 99999 与 0.01 双法命中
    assert result["medium"] >= 1                    # 自然长尾仅 IQR 命中
    groups = {i["group"] for i in result["insufficient"]}
    assert "小众配件" in groups                      # 仅 5 行的小品类
    zero_mad = [
        (col, g["group"]) for col, rep in result["columns"].items()
        for g in rep["groups"] if g.get("mad_zero")
    ]
    assert ("weight", "食品饮料") in zero_mad        # 恒为 0.5kg 的品类
    assert result["min_sample"] == rules["min_group_sample"]
    # 每个命中单元格都带判定依据（中位数 / 边界 / 稳健 z）
    for cell in result["cells"]:
        assert cell["median"] is not None
        assert len(cell["bounds"]) == 2
        assert cell["value"] not in (None, "")
