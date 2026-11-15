# qualiscan · 抽检缺失重复异常值质量评分

上游商品主数据的月度抽检工具：把一份 CSV 交给规则集，真算缺失率、复合键/整行重复、按品类分组的双法异常值，
输出**可复现的百分制分数 + A/B/C/D 分级**，并把同一单元格的多条命中**合并**成一张可直接下发的整改工单。

- 使用者：数据质量组
- 输入：`sample_data/quality_sample.csv`（2000 行 × 9 列、5 个品类）或上传自己的 CSV + `sample_data/rules.json`
- 输出：评分卡（每列每规则违规率）、high/medium 告警（含 row_id/列/当前值/依据/建议）、工单 CSV

## 一、能力（6 个业务模块）

| 模块 | 落地 | 说明 |
|---|---|---|
| 规则集 | `app/loader.py` + `sample_data/rules.json` | 列类型、允许缺失率、权重、取值范围、分组列、阈值；上传时严格校验表头 |
| 数据剖析 | `app/pipeline.py` | 固定 7 阶段流水线，每阶段耗时与产物都可追溯 |
| 重复检测 | `app/duplicates.py` | 复合业务键重复与整行重复分开统计，输出重复组 |
| 异常检测 | `app/outliers.py` | 按品类分组，IQR(1.5×IQR) 与稳健 z(>3.5) 双法，样本 < 8 不判 |
| 评分卡 | `app/score.py` | 每列每规则 `1 − 违规率` 裁剪到 [0,1]，按权重归一为百分制 + 分级 |
| 工单导出 | `app/alerts.py` + `app/export.py` | 同一 `(row_id, column)` 多规则合并一条，`rules[]` 保留集合，导出 CSV |

页面 4 个：`/`（场景、演示步骤、规则集概览）、`/workbench`（样例/上传/粘贴 + 分阶段进度）、`/result/{run_id}`（分数、违规率表、告警依据、样本不足说明）、`/review`（历史、权重编辑与重算、工单下载）。

## 二、核心规则

1. **缺失**：按列缺失率 `missing/total` 与规则 `max_missing_rate` 比较；`allowed`（未超允许，不扣分）/ `violation`（超了）/ `all_empty`（整列全空，最严重）。缺失值识别含 `NA`/`NULL`/`-` 等占位符。
2. **重复**：`business_key`（默认 `product_id + category`）重复 → `duplicate_key`，输出每组行号与额外行数；除 `row_id` 外所有列一致 → `duplicate_row`。整行重复整体归入 `duplicate_row`，不再重复计入 `duplicate_key`（避免同一批行双重扣分）。
3. **异常值**：按 `group_by`（品类）分组；组内有效样本 < `min_group_sample`(8) → `insufficient_sample`，整组不判。IQR 法（Q1−1.5·IQR / Q3+1.5·IQR，线性插值分位）与稳健 z（`|x−median|/(1.4826·MAD) > 3.5`）双法：**双命中 high、单命中 medium**；`MAD = 0` 时稳健 z 不可判（不误报，标注 `mad_zero`）。
4. **评分**：`score = 1 − 违规率` 裁剪到 `[0,1]`；单元权重 = 列权重 × 规则权重；`总分 = 100 × Σ(score×w) / Σw`；`A ≥ 90 / B ≥ 80 / C ≥ 70 / D < 70`。规则无有效样本时按满分计入并在结果页注明。
5. **告警去重**：同一 `(row_id, column)` 只保留一条，`rules[]` 为全部命中规则，`severity` 取最严重一档；缺失违规列按「前 20 行逐行 + 其余按列汇总」输出，避免单列 300 行把工单撑爆。

## 三、启动

```bash
cd projects/gb-630
docker compose config --quiet
docker compose up -d --build
docker compose ps                       # 等 healthy
curl -s http://127.0.0.1:18219/health
```

打开 <http://127.0.0.1:18219/>。数据落在命名卷 `qualiscan-data:/app/data`（SQLite `qualiscan.db`）。
首次启动幂等 seed 一次样例剖析（`settings.seed_version`，重复启动不产生脏数据）。
想重建库：`docker compose down -v` 后再 `up -d`。

容器内跑测试：

```bash
docker compose run --rm app python -m pytest -q
```

## 四、演示步骤

1. 首页看场景、规则集概览、样例埋点说明。
2. `/workbench` 点「执行剖析（样例数据）」→ 页面按 7 阶段显示进度 → 自动跳结果页。
3. 结果页：总分与分级、每列每规则违规率表、缺失明细（allowed / violation / all_empty）、重复组、异常值分组统计（中位数 / MAD / IQR 上下界 / 命中数 / 样本不足）、告警表（同格合并，`rules` 多个标签）。
4. `/review`：改列权重（例如把 `rating` 调到 6）→ 保存 →「按当前权重重算最近一次运行」→ 分数立刻变化；下载工单 CSV。
5. 错误态：上传缺列 CSV（例如删掉 `price`），返回 400 与中文原因，页面可修正后重试。

## 五、API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查（含运行数、规则版本） |
| GET | `/api/rules` | 规则集原文 |
| POST | `/api/runs` | `{"source":"sample"}` 用样例执行，返回 `run_id`（后台线程跑） |
| POST | `/api/runs/upload` | multipart 上传 CSV（UTF-8/BOM/GB18030 自动识别） |
| POST | `/api/runs/text` | `{"csv_text": "..."}` 粘贴执行 |
| GET | `/api/runs/{id}/status` | 进度：`status/stage/stage_index/total_score/grade` |
| GET | `/api/runs/{id}/alerts.csv` | 工单 CSV（UTF-8 BOM，Excel 直接打开） |
| GET | `/api/runs` | 历史运行列表 |
| GET/POST | `/api/rules/weights` | 读取/保存权重覆盖（0~10，超范围 400） |
| POST | `/api/rules/weights/reset` | 恢复默认权重 |
| POST | `/api/runs/{id}/rescore` | 按当前权重重算该次运行总分 |

错误一律 4xx + `{"detail": "中文原因"}`：缺列、含未定义列、表头重复、空文件、只有表头、超 8MB/2 万行、未知运行 404、运行未完成 409。

## 六、目录

```
app/
  main.py         路由（薄）
  settings.py     路径与常量
  loader.py       规则读取、CSV 解析、编码探测、表头契约
  missing.py      缺失规则
  duplicates.py   重复规则
  outliers.py     异常值规则（IQR + 稳健 z）
  validators.py   类型与范围校验
  score.py        评分卡
  alerts.py       告警合并去重
  export.py       工单 CSV
  pipeline.py     7 阶段流水线
  store.py        SQLite（runs / settings）
  seed.py         幂等 seed
  templates/      base/index/workbench/running/result/review
  static/         app.js / style.css
sample_data/      quality_sample.csv、rules.json、_generate.py（固定种子可重生成）
tests/            test_missing / test_duplicates / test_outliers / test_score / test_alerts / test_api
```

## 七、已知限制

- 评分是加权平均，A 级不等于没有问题：总分 93 时仍可能有上百个单元格命中规则，需按工单整改；分级与告警数是两个维度。
- 重复检测的整行签名按原始字符串比较，`0100` 与 `100`、`1.20` 与 `1.2` 视为不同；未做数值归一化（避免误合并）。
- 异常值以「品类」为唯一天然分组；不处理多列联合异常（如 price 与 weight 的比例异常）。
- MAD = 0 的组只做 IQR 判定；若 IQR 也为 0（全部取值相同），该组不会产生异常，仅记录 `mad_zero`。
- 未做登录/权限/审计，工单 CSV 直接下载；上传文件不落盘，只在内存中解析（≤8MB）。
- 后台计算为进程内线程，未引入队列；单实例部署（`docker compose` 一个服务）。
- 缺失违规列只逐行列出前 20 行，其余按列汇总；如需逐行完整清单请导出 CSV 后自行筛选。
