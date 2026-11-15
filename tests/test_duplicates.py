"""重复规则：复合业务键重复与整行重复分开统计，重复组可追溯。"""
from __future__ import annotations

from app import duplicates

COLUMNS = ["row_id", "product_id", "category", "price", "supplier"]
RULES = {"business_key": ["product_id", "category"]}


def row(rid: str, pid: str, cat: str, price: str, sup: str = "S01") -> dict:
    return {"row_id": rid, "product_id": pid, "category": cat, "price": price, "supplier": sup}


def test_composite_key_duplicate_group():
    rows = [
        row("R1", "P1", "数码", "100"),   # 键重复组
        row("R2", "P1", "数码", "120"),
        row("R3", "P1", "数码", "130"),
        row("R4", "P2", "数码", "90"),
    ]
    result = duplicates.analyze(rows, COLUMNS, RULES)
    key = result["duplicate_key"]
    assert key["group_count"] == 1
    assert key["groups"][0]["count"] == 3
    assert key["groups"][0]["extra"] == 2
    assert key["row_ids"] == ["R1", "R2", "R3"]
    assert result["duplicate_row"]["group_count"] == 0


def test_whole_row_duplicate_is_separate_rule():
    rows = [
        row("R1", "P1", "数码", "100"),
        row("R2", "P1", "数码", "100"),   # 除 row_id 外完全相同
        row("R3", "P2", "数码", "100"),
    ]
    result = duplicates.analyze(rows, COLUMNS, RULES)
    assert result["duplicate_row"]["group_count"] == 1
    assert result["duplicate_row"]["row_ids"] == ["R1", "R2"]
    assert result["duplicate_row"]["extra_rows"] == 1
    # 整行重复整体归入 duplicate_row，不再计入 duplicate_key
    assert result["excluded_from_key"] == 2
    assert result["duplicate_key"]["group_count"] == 0
    assert result["duplicate_key"]["extra_rows"] == 0


def test_same_product_different_category_is_not_duplicate():
    rows = [
        row("R1", "P1", "数码", "100"),
        row("R2", "P1", "家居", "100"),
    ]
    result = duplicates.analyze(rows, COLUMNS, RULES)
    assert result["duplicate_key"]["group_count"] == 0
    assert result["duplicate_row"]["group_count"] == 0


def test_extra_rows_and_group_payload():
    # 同键但供应商不同 → 不是整行重复，属复合键重复
    rows = [row("R1", "P1", "数码", "100", "S01"),
            row("R2", "P1", "数码", "100", "S02"),
            row("R3", "P1", "数码", "100", "S03")]
    result = duplicates.analyze(rows, COLUMNS, RULES)
    group = result["duplicate_key"]["groups"][0]
    assert group["extra"] == 2
    assert "product_id" in group["key"] and group["key"]["product_id"] == "P1"
    assert group["label"].startswith("业务键")


def test_custom_business_key(rules):
    rows = [
        {"row_id": "R1", "supplier": "S01", "price": "100"},
        {"row_id": "R2", "supplier": "S01", "price": "120"},
        {"row_id": "R3", "supplier": "S02", "price": "100"},
    ]
    result = duplicates.analyze(rows, ["row_id", "supplier", "price"],
                                {"business_key": ["supplier"]})
    assert result["business_key"] == ["supplier"]
    assert result["duplicate_key"]["group_count"] == 1
    assert result["duplicate_key"]["row_count"] == 2


def test_sample_data_has_both_kinds(dataset, rules):
    result = duplicates.analyze(dataset["rows"], dataset["columns"], rules)
    key = result["duplicate_key"]
    row_dup = result["duplicate_row"]
    assert key["group_count"] == 1
    assert key["groups"][0]["key"]["product_id"] == "P00009"
    assert key["groups"][0]["row_ids"] == ["R000009", "R000010", "R000011"]
    assert row_dup["group_count"] == 1
    assert row_dup["row_ids"] == ["R000020", "R000021"]
    assert result["excluded_from_key"] == 2
    # 两类重复的额外行数分别可查
    assert key["extra_rows"] == 2
    assert row_dup["extra_rows"] == 1
    assert result["total_extra_rows"] == 3
