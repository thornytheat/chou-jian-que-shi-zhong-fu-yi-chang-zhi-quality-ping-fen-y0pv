"""记录级隔离：坏记录带原因被隔离，健康记录正常进入下游，恒等式始终成立。"""
from __future__ import annotations

from app import quarantine
from app.loader import DEFAULT_MISSING_TOKENS

HEADER = "row_id,product_id,category,price\n"
TOKENS = tuple(DEFAULT_MISSING_TOKENS)
COLUMNS = ["row_id", "product_id", "category", "price"]


def test_all_healthy_records():
    text = HEADER + "R1,P1,数码,10\nR2,P2,数码,20\n"
    intake = quarantine.parse_records(text, COLUMNS, TOKENS)
    assert intake.source_count == 2
    assert intake.healthy_count == 2
    assert intake.quarantine_count == 0
    assert [r["row_id"] for r in intake.healthy_rows] == ["R1", "R2"]


def test_blank_line_is_quarantined_not_silently_skipped():
    text = HEADER + "R1,P1,数码,10\n\n   \nR2,P2,数码,20\n"
    intake = quarantine.parse_records(text, COLUMNS, TOKENS)
    assert intake.source_count == 4  # 两行健康 + 两行空白，全部计入源记录
    assert intake.healthy_count == 2
    bad = intake.bad_records
    assert len(bad) == 2
    assert all(b.bad_reason == "blank_line" for b in bad)
    assert "空白行" in bad[0].bad_detail


def test_field_count_mismatch_is_quarantined_with_raw_fields():
    text = HEADER + "R1,P1,数码,10\nR2,P2,数码,10,EXTRA,EXTRA2\nR3,P3,数码,10\n"
    intake = quarantine.parse_records(text, COLUMNS, TOKENS)
    bad = intake.bad_records
    assert len(bad) == 1
    assert bad[0].bad_reason == "field_count_mismatch"
    assert bad[0].raw_fields == ["R2", "P2", "数码", "10", "EXTRA", "EXTRA2"]
    assert "实际 6 个字段" in bad[0].bad_detail and "表头要求 4 个" in bad[0].bad_detail
    assert intake.healthy_count == 2


def test_missing_row_id_including_placeholder_tokens():
    text = HEADER + "R1,P1,数码,10\nNA,P2,数码,10\n-,P3,数码,10\n,P4,数码,10\n"
    intake = quarantine.parse_records(text, COLUMNS, TOKENS)
    bad = intake.bad_records
    assert [b.bad_reason for b in bad] == ["missing_row_id"] * 3
    assert intake.healthy_count == 1
    assert intake.healthy_rows[0]["row_id"] == "R1"


def test_duplicate_row_id_first_kept_rest_quarantined():
    text = HEADER + "R1,P1,数码,10\nR2,P2,数码,20\nR1,P3,数码,30\nR1,P4,数码,40\n"
    intake = quarantine.parse_records(text, COLUMNS, TOKENS)
    assert intake.healthy_count == 2
    dup = [b for b in intake.bad_records if b.bad_reason == "duplicate_row_id"]
    assert len(dup) == 2
    assert all("R1" in b.bad_detail for b in dup)
    # 首次出现的 R1 仍是健康记录
    assert intake.healthy_rows[0]["row_id"] == "R1"


def test_identity_always_holds():
    text = HEADER + (
        "R1,P1,数码,10\n"
        "\n"
        "R2,P2,数码,10,EXTRA\n"
        ",P3,数码,10\n"
        "R1,P4,数码,10\n"
        "R3,P5,数码,10\n"
    )
    intake = quarantine.parse_records(text, COLUMNS, TOKENS)
    assert intake.source_count == intake.healthy_count + intake.quarantine_count
    assert (intake.source_count, intake.healthy_count, intake.quarantine_count) == (6, 2, 4)


def test_cell_level_quality_issues_are_not_bad_records():
    # 单元格层面的类型/缺失问题不是坏记录：仍属健康行，交给下游规则判定
    text = HEADER + "R1,P1,数码,面议\nR2,,数码,\n"
    intake = quarantine.parse_records(text, COLUMNS, TOKENS)
    assert intake.quarantine_count == 0
    assert intake.healthy_count == 2


def test_line_numbers_point_to_source_file():
    text = HEADER + "R1,P1,数码,10\n\nR2,P2,数码,10\n"
    intake = quarantine.parse_records(text, COLUMNS, TOKENS)
    blank = next(b for b in intake.bad_records if b.bad_reason == "blank_line")
    assert blank.line_no == 3  # 表头第 1 行、R1 第 2 行、空白物理行第 3 行


def test_reason_labels_cover_all_codes():
    text = HEADER + "R1,P1,数码,10\n"
    intake = quarantine.parse_records(text, COLUMNS, TOKENS)
    for code in quarantine.REASON_LABELS:
        assert quarantine.reason_label(code)
    assert intake.source_count == 1
