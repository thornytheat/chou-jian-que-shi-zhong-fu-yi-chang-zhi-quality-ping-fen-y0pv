#!/usr/bin/env python3
"""生成 sample_data/quality_sample.csv（2000 行 × 9 列，5 个品类）。

固定随机种子，可重复生成；文件已随仓库提交，正常使用不需要运行本脚本。
故意埋入：3 类缺失（允许缺失列 / 违规缺失列 / 全空列）、1 组复合业务键重复、
1 组整行重复、按品类分布的异常值、1 个仅 5 行的小品类、越界值与类型错误。
"""
from __future__ import annotations

import csv
import pathlib
import random

SEED = 630
COLUMNS = [
    "row_id", "product_id", "category", "price", "stock",
    "weight", "supplier", "updated_at", "rating",
]

# (品类, 行数, 价格中位, 价格半宽, 库存形态, 重量形态)
CATEGORIES = [
    ("数码配件", 700, 320.0, 140.0, "uniform", "normal"),
    ("家居日用", 600, 95.0, 30.0, "uniform", "normal"),
    ("食品饮料", 400, 25.0, 8.0, "uniform", "constant"),
    ("服饰鞋包", 295, 180.0, 60.0, "uniform", "normal"),
    ("小众配件", 5, 60.0, 15.0, "uniform", "normal"),
]
SUPPLIERS = [f"S{i:02d}" for i in range(1, 21)]


def main() -> None:
    rnd = random.Random(SEED)
    rows: list[dict[str, str]] = []
    seq = 0
    for cat, count, price_mid, price_half, stock_shape, weight_shape in CATEGORIES:
        for _ in range(count):
            seq += 1
            price = max(1.0, round(rnd.gauss(price_mid, price_half / 2.0), 2))
            stock = rnd.randint(5, 500)
            if weight_shape == "constant":
                weight = 0.5
            else:
                weight = round(max(0.05, rnd.gauss(1.2, 0.35)), 2)
            month = rnd.choice(["09", "10"])
            day = rnd.randint(1, 28)
            rows.append({
                "row_id": f"R{seq:06d}",
                "product_id": f"P{seq:05d}",
                "category": cat,
                "price": f"{price:.2f}",
                "stock": str(stock),
                "weight": f"{weight:.2f}",
                "supplier": rnd.choice(SUPPLIERS),
                "updated_at": f"2026-{month}-{day:02d}",
                "rating": "",
            })

    # --- 缺失：supplier 允许缺失（约 18%），weight 违规缺失（约 9%），rating 全空 ---
    for row in rows:
        if rnd.random() < 0.18:
            row["supplier"] = ""      # 允许缺失（允许 25%）
    for row in rows:
        if rnd.random() < 0.14:
            row["weight"] = ""        # 违规缺失（允许 5%）
        if rnd.random() < 0.04:
            row["price"] = ""         # 违规缺失（允许 2%）
        if rnd.random() < 0.05:
            row["stock"] = ""         # 违规缺失（允许 2%）
        if rnd.random() < 0.04:
            row["updated_at"] = ""    # 违规缺失（允许 1%）
        if rnd.random() < 0.006:
            row["category"] = ""      # 违规缺失（允许 0%）

    # --- 复合业务键重复：R000010/R000011 复用 R000009 的 product_id（价格不同，不是整行重复）---
    for i in (9, 10):
        rows[i]["product_id"] = rows[8]["product_id"]
        rows[i]["price"] = f"{100.0 + i * 7.5:.2f}"
        rows[i]["supplier"] = "S03"

    # --- 整行重复：R000021 的全部业务列复制 R000020（row_id 除外）---
    src, dst = rows[19], rows[20]
    for col in COLUMNS:
        if col != "row_id":
            dst[col] = src[col]

    # --- 越界 / 类型错误 / 单法异常值 ---
    rows[30]["price"] = "99999.00"          # 数码配件：越界 + IQR/稳健z 双法命中 → high
    rows[31]["stock"] = "-5"                # 数码配件：仅越界
    rows[700]["price"] = "0.01"             # 家居日用：越界 + 双法命中 → high
    rows[701]["weight"] = "0"               # 家居日用：仅越界
    rows[1310]["stock"] = "待定"             # 食品饮料：类型错误（非数字、非缺失）
    rows[1311]["price"] = "面议"             # 食品饮料：类型错误
    rows[1312]["updated_at"] = "2026-13-45"  # 食品饮料：非法月份
    rows[1313]["updated_at"] = "2026-02-30"  # 食品饮料：不存在的日

    out = pathlib.Path(__file__).with_name("quality_sample.csv")
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {out} rows={len(rows)} cols={len(COLUMNS)}")


if __name__ == "__main__":
    main()
