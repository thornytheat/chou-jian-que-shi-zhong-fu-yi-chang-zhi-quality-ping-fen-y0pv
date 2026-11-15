"""异常值检测：按分组列（品类）分组，IQR(1.5×IQR) 与稳健 z-score 双法。

判定口径：
- 组内有效样本 < min_group_sample（默认 8）→ insufficient_sample，整组不判异常。
- IQR 法：low = Q1 − k·IQR，high = Q3 + k·IQR（k 默认 1.5，线性插值分位数）。
- 稳健 z：|x − median| / (1.4826 · MAD) > 3.5。MAD = 0（组内过半取值相同）时该法不可用，
  记 mad_zero=True 且不产生 MAD 命中（边界口径，避免除零）。
- 分级：两法都命中 → high；仅一法命中 → medium。
"""
from __future__ import annotations

from .loader import to_float

DEFAULT_MIN_SAMPLE = 8
DEFAULT_IQR_K = 1.5
DEFAULT_MAD_K = 3.5
MAD_SCALE = 1.4826


def quantile(sorted_values: list[float], q: float) -> float:
    """线性插值分位数（与常见统计包 linear 口径一致）。"""
    if not sorted_values:
        raise ValueError("empty sample")
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q
    low = int(pos)
    high = min(low + 1, len(sorted_values) - 1)
    frac = pos - low
    return sorted_values[low] * (1 - frac) + sorted_values[high] * frac


def median(values: list[float]) -> float:
    return quantile(sorted(values), 0.5)


def iqr_bounds(values: list[float], k: float = DEFAULT_IQR_K) -> dict:
    ordered = sorted(values)
    q1 = quantile(ordered, 0.25)
    q3 = quantile(ordered, 0.75)
    iqr = q3 - q1
    return {
        "q1": q1,
        "q3": q3,
        "iqr": iqr,
        "low": q1 - k * iqr,
        "high": q3 + k * iqr,
    }


def mad(values: list[float]) -> float:
    med = median(values)
    return median([abs(v - med) for v in values])


def robust_z(value: float, med: float, mad_value: float) -> float | None:
    """MAD=0 时不可判定，返回 None（不产生命中）。"""
    if mad_value <= 0:
        return None
    return abs(value - med) / (MAD_SCALE * mad_value)


def group_stats(values: list[float], params: dict) -> dict:
    bounds = iqr_bounds(values, params["iqr_k"])
    med = median(values)
    mad_value = mad(values)
    return {
        "sample": len(values),
        "median": round(med, 4),
        "mad": round(mad_value, 4),
        "q1": round(bounds["q1"], 4),
        "q3": round(bounds["q3"], 4),
        "iqr": round(bounds["iqr"], 4),
        "low": round(bounds["low"], 4),
        "high": round(bounds["high"], 4),
        "mad_zero": mad_value <= 0,
    }


def classify(value: float, values: list[float], params: dict) -> dict:
    """返回单值判定：iqr_hit / mad_hit / severity(None|medium|high)。"""
    bounds = iqr_bounds(values, params["iqr_k"])
    med = median(values)
    mad_value = mad(values)
    iqr_hit = value < bounds["low"] or value > bounds["high"]
    z = robust_z(value, med, mad_value)
    mad_hit = z is not None and z > params["mad_k"]
    severity = None
    if iqr_hit and mad_hit:
        severity = params["dual_hit_severity"]
    elif iqr_hit or mad_hit:
        severity = params["single_hit_severity"]
    return {
        "iqr_hit": iqr_hit,
        "mad_hit": mad_hit,
        "z": None if z is None else round(z, 4),
        "severity": severity,
        "mad_zero": mad_value <= 0,
        "median": med,
        "mad": mad_value,
        "low": bounds["low"],
        "high": bounds["high"],
    }


def params_from_rules(rules: dict) -> dict:
    spec = rules.get("outlier") or {}
    return {
        "iqr_k": float(spec.get("iqr_k", DEFAULT_IQR_K)),
        "mad_k": float(spec.get("mad_k", DEFAULT_MAD_K)),
        "dual_hit_severity": spec.get("dual_hit_severity", "high"),
        "single_hit_severity": spec.get("single_hit_severity", "medium"),
        "min_sample": int(rules.get("min_group_sample", DEFAULT_MIN_SAMPLE)),
    }


def analyze(rows: list[dict[str, str]], columns: list[str], rules: dict) -> dict:
    params = params_from_rules(rules)
    group_by = rules.get("group_by") or "category"
    min_sample = params["min_sample"]
    column_reports = {}
    cells: list[dict] = []
    insufficient: list[dict] = []

    for column in columns:
        spec = rules["columns"].get(column, {})
        if "outlier" not in spec.get("rules", []):
            continue
        groups: dict[str, list[dict[str, str]]] = {}
        ungrouped = 0
        for row in rows:
            key = str(row.get(group_by, "") or "").strip()
            if not key:
                # 分组键缺失的行无法归组，不参与分组异常判定（已由缺失规则单独命中）
                ungrouped += 1
                continue
            groups.setdefault(key, []).append(row)

        column_cells: list[dict] = []
        group_reports: list[dict] = []
        evaluated = 0
        skipped = 0
        for key in sorted(groups):
            members = groups[key]
            pairs = []
            for row in members:
                value = to_float(row.get(column, ""))
                if value is not None:
                    pairs.append((row, value))
            values = [v for _, v in pairs]
            if len(values) < min_sample:
                stats = {
                    "group": key,
                    "sample": len(values),
                    "status": "insufficient_sample",
                    "note": f"组内有效样本 {len(values)} < {min_sample}，不判异常",
                }
                group_reports.append(stats)
                insufficient.append({
                    "column": column,
                    "group": key,
                    "sample": len(values),
                    "min_sample": min_sample,
                    "note": stats["note"],
                })
                skipped += len(values)
                continue
            stats = group_stats(values, params)
            stats["group"] = key
            stats["status"] = "evaluated"
            stats["outlier_count"] = 0
            for row, value in pairs:
                verdict = classify(value, values, params)
                if verdict["severity"] is None:
                    continue
                stats["outlier_count"] += 1
                column_cells.append({
                    "row_id": row["row_id"],
                    "column": column,
                    "group": key,
                    "value": value,
                    "severity": verdict["severity"],
                    "iqr_hit": verdict["iqr_hit"],
                    "mad_hit": verdict["mad_hit"],
                    "robust_z": verdict["z"],
                    "mad_zero": verdict["mad_zero"],
                    "bounds": [round(verdict["low"], 4), round(verdict["high"], 4)],
                    "median": round(verdict["median"], 4),
                    "mad": round(verdict["mad"], 4),
                    "methods": _methods(verdict),
                })
            evaluated += len(values)
            group_reports.append(stats)
        column_cells.sort(key=lambda c: (c["severity"] != "high", c["row_id"]))
        column_reports[column] = {
            "column": column,
            "ungrouped_skipped": ungrouped,
            "groups": group_reports,
            "cells": column_cells,
            "evaluated_sample": evaluated,
            "skipped_sample": skipped,
            "high": sum(1 for c in column_cells if c["severity"] == "high"),
            "medium": sum(1 for c in column_cells if c["severity"] == "medium"),
        }
        cells.extend(column_cells)

    cells.sort(key=lambda c: (c["severity"] != "high", c["column"], c["row_id"]))
    return {
        "group_by": group_by,
        "params": params,
        "min_sample": min_sample,
        "columns": column_reports,
        "cells": cells,
        "insufficient": insufficient,
        "high": sum(1 for c in cells if c["severity"] == "high"),
        "medium": sum(1 for c in cells if c["severity"] == "medium"),
        "evaluated_columns": sorted(column_reports),
    }


def _methods(verdict: dict) -> list[str]:
    names = []
    if verdict["iqr_hit"]:
        names.append("IQR")
    if verdict["mad_hit"]:
        names.append("稳健z")
    return names
