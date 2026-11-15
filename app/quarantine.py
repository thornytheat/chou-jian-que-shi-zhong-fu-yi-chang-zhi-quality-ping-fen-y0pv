"""坏记录隔离：把不可解析/不可标识的记录从健康数据流中剥离，并写清原因。

口径（记录级，不再静默跳过任何一行）：
- 空白物理行（连一个非空字段都没有）→ blank_line。
- 字段数与表头不一致 → field_count_mismatch（保留原始字段，不按列名猜测）。
- row_id 列缺失/为占位符空值 → missing_row_id（无法定位的行不允许进入分析）。
- row_id 在同一文件内重复 → duplicate_row_id（首次出现保留，后续出现隔离）。

注意：单元格层面的质量问题（数字列写了「面议」、越界、缺失、异常值等）是**健康记录上的告警**，
由 validators/missing/outliers 模块判定，不属于坏记录，不在这里隔离。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

# 原因码 → 中文说明（页面与 CSV 共用同一份措辞）
REASON_LABELS = {
    "blank_line": "空白行：该行没有任何非空字段",
    "field_count_mismatch": "字段数与表头不一致：无法按列名对齐，禁止猜测填充",
    "missing_row_id": "row_id 为空或为占位符：无法定位该行，不能进入剖析",
    "duplicate_row_id": "row_id 与文件内更早的记录重复：该行不能再次计数",
}


@dataclass
class ParsedRecord:
    line_no: int
    raw_fields: list[str]
    row: dict[str, str] | None = None
    bad_reason: str | None = None
    bad_detail: str = ""

    @property
    def is_healthy(self) -> bool:
        return self.bad_reason is None and self.row is not None


@dataclass
class IntakeResult:
    columns: list[str]
    records: list[ParsedRecord] = field(default_factory=list)

    @property
    def healthy_rows(self) -> list[dict[str, str]]:
        return [rec.row for rec in self.records if rec.is_healthy]

    @property
    def bad_records(self) -> list[ParsedRecord]:
        return [rec for rec in self.records if not rec.is_healthy]

    @property
    def source_count(self) -> int:
        return len(self.records)

    @property
    def healthy_count(self) -> int:
        return sum(1 for r in self.records if r.is_healthy)

    @property
    def quarantine_count(self) -> int:
        return sum(1 for r in self.records if not r.is_healthy)


def is_blank(fields: list[str]) -> bool:
    return not any((f or "").strip() for f in fields)


def parse_records(text: str, columns: list[str], tokens: tuple[str, ...]) -> IntakeResult:
    """按物理行解析 CSV 文本，逐记录判定健康/隔离。

    表头已由上层校验过（重复列名、缺列等仍是 400 级输入错误），这里只做记录级隔离。
    """
    reader = csv.reader(io.StringIO(text))
    result = IntakeResult(columns=list(columns))
    seen_ids: set[str] = set()
    header_seen = False
    expected = len(columns)
    token_set = {t.strip().lower() for t in tokens}

    for raw_fields in reader:
        if not header_seen:
            header_seen = True
            continue  # 表头行：列名契约由 loader.validate_columns 负责
        line_no = reader.line_num

        if is_blank(raw_fields):
            result.records.append(ParsedRecord(
                line_no=line_no, raw_fields=list(raw_fields),
                bad_reason="blank_line", bad_detail=REASON_LABELS["blank_line"],
            ))
            continue

        if len(raw_fields) != expected:
            result.records.append(ParsedRecord(
                line_no=line_no, raw_fields=list(raw_fields),
                bad_reason="field_count_mismatch",
                bad_detail=f"实际 {len(raw_fields)} 个字段，表头要求 {expected} 个；{REASON_LABELS['field_count_mismatch']}",
            ))
            continue

        row = {col: ("" if raw_fields[i] is None else str(raw_fields[i]).strip())
               for i, col in enumerate(columns)}
        row_id = (row.get("row_id") or "").strip()
        if not row_id or row_id.lower() in token_set:
            result.records.append(ParsedRecord(
                line_no=line_no, raw_fields=list(raw_fields), row=row,
                bad_reason="missing_row_id", bad_detail=REASON_LABELS["missing_row_id"],
            ))
            continue
        if row_id in seen_ids:
            result.records.append(ParsedRecord(
                line_no=line_no, raw_fields=list(raw_fields), row=row,
                bad_reason="duplicate_row_id",
                bad_detail=f"row_id={row_id} 已在更早的记录中出现；{REASON_LABELS['duplicate_row_id']}",
            ))
            continue

        seen_ids.add(row_id)
        result.records.append(ParsedRecord(line_no=line_no, raw_fields=list(raw_fields), row=row))

    return result


def reason_label(code: str) -> str:
    return REASON_LABELS.get(code, code)
