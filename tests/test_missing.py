"""缺失规则：允许缺失 vs 违规、全空列、缺失率边界与占位符识别。"""
from __future__ import annotations

from app import missing

COLUMNS = ["row_id", "supplier", "weight", "rating"]


def make_rules(**max_rates) -> dict:
    base = {"supplier": 0.25, "weight": 0.05, "rating": 0.02, "row_id": 0.0}
    base.update(max_rates)
    return {
        "missing_tokens": ["", "NA", "N/A", "NULL", "null", "-"],
        "columns": {
            name: {"rules": ["missing"], "max_missing_rate": base[name]}
            for name in COLUMNS
        },
    }


def rows_of(supplier: list[str], weight: list[str], rating: list[str]) -> list[dict]:
    rows = []
    for i, (s, w, r) in enumerate(zip(supplier, weight, rating), start=1):
        rows.append({"row_id": f"R{i:03d}", "supplier": s, "weight": w, "rating": r})
    return rows


def test_allowed_vs_violation_and_all_empty(rules):
    data = [
        {"row_id": "R001", "supplier": "", "weight": "1.2", "rating": ""},
        {"row_id": "R002", "supplier": "S01", "weight": "", "rating": ""},
        {"row_id": "R003", "supplier": "S02", "weight": "1.1", "rating": ""},
        {"row_id": "R004", "supplier": "S03", "weight": "1.3", "rating": ""},
    ]
    result = missing.analyze(data, COLUMNS, make_rules())
    by_col = {item["column"]: item for item in result["columns"]}
    # supplier 缺失 1/4 = 25% == 允许 25% → allowed
    assert by_col["supplier"]["rate"] == 0.25
    assert by_col["supplier"]["status"] == "allowed"
    # weight 缺失 1/4 = 25% > 允许 5% → violation
    assert by_col["weight"]["status"] == "violation"
    assert by_col["weight"]["missing"] == 1
    # rating 整列全空 → all_empty（优先级高于 violation）
    assert by_col["rating"]["status"] == "all_empty"
    assert by_col["rating"]["all_empty"] is True
    # 违规列表与允许列表分开统计
    assert [i["column"] for i in result["violations"]] == ["weight"]
    assert [i["column"] for i in result["allowed"]] == ["row_id", "supplier"]
    assert result["total_missing_cells"] == 1 + 1 + 4


def test_rate_boundary_is_inclusive(rules):
    """缺失率恰好等于允许值时必须判 allowed（不允许用 > 做模糊比较）。"""
    data = [{"row_id": f"R{i}", "supplier": "", "weight": "1", "rating": "5"} for i in range(1, 5)]
    data += [{"row_id": f"R{i}", "supplier": "S01", "weight": "1", "rating": "5"} for i in range(5, 9)]
    result = missing.analyze(data, COLUMNS, make_rules(supplier=0.5))
    item = next(i for i in result["columns"] if i["column"] == "supplier")
    assert item["rate"] == 0.5
    assert item["status"] == "allowed"

    stricter = missing.analyze(data, COLUMNS, make_rules(supplier=0.49))
    item2 = next(i for i in stricter["columns"] if i["column"] == "supplier")
    assert item2["status"] == "violation"


def test_placeholder_tokens_counted_as_missing():
    data = [
        {"row_id": "R1", "supplier": "NA", "weight": "-", "rating": "null"},
        {"row_id": "R2", "supplier": "S01", "weight": "1.0", "rating": "5"},
    ]
    result = missing.analyze(data, COLUMNS, make_rules())
    by_col = {item["column"]: item for item in result["columns"]}
    assert by_col["supplier"]["missing"] == 1
    assert by_col["weight"]["missing"] == 1
    assert by_col["rating"]["missing"] == 1
    assert by_col["rating"]["status"] == "violation"


def test_zero_missing_column_is_allowed():
    data = [{"row_id": f"R{i}", "supplier": "S", "weight": "1", "rating": "5"} for i in range(3)]
    result = missing.analyze(data, COLUMNS, make_rules())
    item = next(i for i in result["columns"] if i["column"] == "weight")
    assert item["missing"] == 0
    assert item["status"] == "allowed"
    assert result["all_empty"] == []


def test_sample_rows_and_notes(dataset, rules):
    result = missing.analyze(dataset["rows"], dataset["columns"], rules)
    by_col = {item["column"]: item for item in result["columns"]}
    assert by_col["supplier"]["status"] == "allowed"       # 样例：允许缺失列
    assert by_col["weight"]["status"] == "violation"       # 样例：违规缺失列
    assert by_col["rating"]["status"] == "all_empty"       # 样例：全空列
    assert by_col["rating"]["missing"] == 2000
    assert "允许缺失" in by_col["supplier"]["note"]
    assert len(by_col["weight"]["sample_row_ids"]) == 20   # 证据行号有上限
    assert result["total_rows"] == 2000
