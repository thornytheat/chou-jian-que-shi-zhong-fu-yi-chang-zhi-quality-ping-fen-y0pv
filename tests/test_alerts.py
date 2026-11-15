"""告警规则：同一 (row_id, column) 多规则命中合并为一条，保留 rules[] 集合。"""
from __future__ import annotations

from app import alerts


def missing_item(column: str, row_ids: list[str], status: str = "violation",
                 rate: float = 0.2, max_rate: float = 0.05, missing: int = 0) -> dict:
    return {
        "column": column,
        "total": 100,
        "missing": missing or len(row_ids),
        "rate": rate,
        "max_rate": max_rate,
        "status": status,
        "sample_row_ids": row_ids,
    }


def base_report(*, missing_items, outlier_cells, range_cells=(), type_cells=(),
                key_groups=(), row_groups=()):
    return (
        {"total_rows": 100, "columns": list(missing_items), "total_missing_cells": 0},
        {
            "business_key": ["product_id", "category"],
            "duplicate_key": {
                "columns": ["product_id", "category"], "groups": list(key_groups),
                "group_count": len(key_groups),
                "row_count": sum(g["count"] for g in key_groups),
                "extra_rows": sum(g["extra"] for g in key_groups),
                "row_ids": [],
            },
            "duplicate_row": {
                "columns": ["product_id"], "groups": list(row_groups),
                "group_count": len(row_groups),
                "row_count": sum(g["count"] for g in row_groups),
                "extra_rows": sum(g["extra"] for g in row_groups),
                "row_ids": [],
            },
            "excluded_from_key": 0,
        },
        {"cells": list(outlier_cells), "insufficient": []},
        {"range": {"cells": list(range_cells), "count": len(range_cells)},
         "type": {"cells": list(type_cells), "count": len(type_cells)}},
    )


def outlier_cell(row_id: str, column: str, severity: str = "high",
                 iqr: bool = True, mad: bool = True) -> dict:
    return {
        "row_id": row_id, "column": column, "group": "数码配件", "value": 99999.0,
        "severity": severity, "iqr_hit": iqr, "mad_hit": mad, "robust_z": 42.0,
        "mad_zero": False, "bounds": [100.0, 500.0], "median": 300.0, "mad": 50.0,
        "methods": [],
    }


def test_same_cell_merged_into_one_alert():
    miss, dup, out, val = base_report(
        missing_items=[],
        outlier_cells=[outlier_cell("R1", "price")],
        range_cells=[{"row_id": "R1", "column": "price", "value": 99999.0,
                      "raw": "99999.00", "range": [0.5, 50000],
                      "reason": "取值 99999 超出允许范围 [0.5, 50000]"}],
    )
    result = alerts.build(miss, dup, out, val)
    assert result["total"] == 1
    item = result["items"][0]
    assert item["row_id"] == "R1" and item["column"] == "price"
    assert item["rules"] == ["range", "outlier_mad", "outlier_iqr"]
    assert item["merged_from"] == 3
    assert result["merged_cells"] == 1
    # severity 取最严重的一档
    assert item["severity"] == "high"
    # 依据合并保留多条，方便一眼看到为什么判
    assert "超出允许范围" in item["reason"] and "稳健 z" in item["reason"]
    assert item["suggestion"]


def test_different_cells_are_not_merged():
    miss, dup, out, val = base_report(
        missing_items=[],
        outlier_cells=[outlier_cell("R1", "price"), outlier_cell("R2", "price"),
                       outlier_cell("R1", "stock")],
    )
    result = alerts.build(miss, dup, out, val)
    keys = {(i["row_id"], i["column"]) for i in result["items"]}
    assert len(keys) == 3
    assert result["total"] == 3
    assert result["merged_cells"] == 3   # 每个单元格都有 IQR + 稳健z 两条


def test_severity_takes_max_of_mixed_rules():
    miss, dup, out, val = base_report(
        missing_items=[missing_item("weight", ["R9"])],
        outlier_cells=[outlier_cell("R9", "weight", severity="medium", iqr=True, mad=False)],
    )
    result = alerts.build(miss, dup, out, val)
    assert result["total"] == 1
    item = result["items"][0]
    assert set(item["rules"]) == {"missing", "outlier_iqr"}
    assert item["severity"] == "medium"


def test_all_empty_column_produces_single_column_alert():
    miss, dup, out, val = base_report(
        missing_items=[missing_item("rating", [], status="all_empty", rate=1.0,
                                    max_rate=0.02, missing=100)],
        outlier_cells=[],
    )
    result = alerts.build(miss, dup, out, val)
    assert result["total"] == 1
    item = result["items"][0]
    assert item["row_id"] == "—"
    assert item["column"] == "rating"
    assert item["scope"] == "column"
    assert item["severity"] == "high"
    assert item["rows_affected"] == 100


def test_missing_alert_capped_with_column_summary():
    row_ids = [f"R{i:03d}" for i in range(1, 101)]   # 100 行缺失
    miss, dup, out, val = base_report(
        missing_items=[missing_item("weight", row_ids, missing=100)],
        outlier_cells=[],
    )
    result = alerts.build(miss, dup, out, val)
    cell_alerts = [i for i in result["items"] if i["scope"] == "cell"]
    column_alerts = [i for i in result["items"] if i["scope"] == "column"]
    assert len(cell_alerts) == alerts.MISSING_SAMPLE_LIMIT == 20
    assert len(column_alerts) == 1
    assert column_alerts[0]["rows_affected"] == 100
    assert "其余按列汇总" in column_alerts[0]["reason"]


def test_duplicate_key_alerts_use_key_columns():
    group = {"key": {"product_id": "P1", "category": "数码"},
             "count": 3, "extra": 2, "row_ids": ["R1", "R2", "R3"],
             "label": "业务键 product_id=P1 + category=数码"}
    miss, dup, out, val = base_report(missing_items=[], outlier_cells=[], key_groups=[group])
    result = alerts.build(miss, dup, out, val)
    assert result["total"] == 6                  # 3 行 × 2 个键列
    assert all(i["rules"] == ["duplicate_key"] for i in result["items"])
    assert {i["column"] for i in result["items"]} == {"product_id", "category"}
    assert all("P1" in i["reason"] for i in result["items"])


def test_duplicate_row_alert_scope():
    group = {"key": {"product_id": "P1"}, "count": 2, "extra": 1,
             "row_ids": ["R1", "R2"], "label": "整行"}
    miss, dup, out, val = base_report(missing_items=[], outlier_cells=[], row_groups=[group])
    result = alerts.build(miss, dup, out, val)
    assert result["total"] == 2
    assert all(i["scope"] == "row" for i in result["items"])
    assert all(i["column"] == "（整行）" for i in result["items"])
    assert result["high"] == 2


def test_by_rule_counts_and_sorting():
    miss, dup, out, val = base_report(
        missing_items=[missing_item("weight", ["R1"])],
        outlier_cells=[outlier_cell("R1", "price")],
        type_cells=[{"row_id": "R2", "column": "price", "value": "面议",
                     "expected": "数字", "reason": "「面议」不是合法数字"}],
    )
    result = alerts.build(miss, dup, out, val)
    assert result["by_rule"]["outlier_iqr"] == 1
    assert result["by_rule"]["outlier_mad"] == 1
    assert result["by_rule"]["missing"] == 1
    assert result["by_rule"]["type"] == 1
    # high 排在前
    assert result["items"][0]["severity"] == "high"
    assert result["by_severity"]["high"] == result["high"]


def test_sample_data_alerts_merged_once_per_cell(dataset, rules):
    from app import duplicates, missing, outliers, validators
    from app.loader import missing_tokens

    tokens = missing_tokens(rules)
    miss = missing.analyze(dataset["rows"], dataset["columns"], rules)
    dup = duplicates.analyze(dataset["rows"], dataset["columns"], rules)
    out = outliers.analyze(dataset["rows"], dataset["columns"], rules)
    val = validators.analyze(dataset["rows"], dataset["columns"], rules, tokens)
    result = alerts.build(miss, dup, out, val)

    keys = [(i["row_id"], i["column"]) for i in result["items"]]
    assert len(keys) == len(set(keys))            # 同一单元格只出现一次
    merged = [i for i in result["items"] if i["merged_from"] > 1]
    assert merged, "样例里应有同格多规则命中的单元格"
    target = next(i for i in result["items"] if i["row_id"] == "R000031" and i["column"] == "price")
    assert set(target["rules"]) == {"range", "outlier_iqr", "outlier_mad"}
    assert target["severity"] == "high"
