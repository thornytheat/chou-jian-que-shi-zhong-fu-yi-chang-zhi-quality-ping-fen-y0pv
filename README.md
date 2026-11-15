# qualiscan · 抽检缺失重复异常值质量评分

上游商品主数据的月度抽检工具：把一份 CSV 交给规则集，**按数据分片导入**，坏记录单独隔离并注明原因，
真算缺失率、复合键/整行重复、按品类分组的双法异常值，全部分片核对相符后**统一发布**可复现的
百分制分数 + A/B/C/D 分级，并把同一单元格的多条命中**合并**成一张可直接下发的整改工单。

- 使用者：数据质量组
- 输入：`sample_data/quality_sample.csv`（2000 行 × 9 列、5 个品类）或上传自己的 CSV + `sample_data/rules.json`
- 输出：评分卡（每列每规则违规率）、high/medium 告警（含 row_id/列/当前值/依据/建议）、工单 CSV、隔离记录 CSV

## 一、能力（8 个业务模块）

| 模块 | 落地 | 说明 |
|---|---|---|
| 规则集 | `app/loader.py` + `sample_data/rules.json` | 列类型、允许缺失率、权重、取值范围、分组列、阈值；上传时严格校验表头 |
| 坏记录隔离 | `app/quarantine.py` | 记录级判定：空白行 / 字段数不符 / row_id 缺失 / row_id 重复，逐条带原因码、行号与原始内容，**不静默跳过** |
| 分片作业 | `app/job.py` | 切片确认、检查点、失败续跑、故障屏障；已确认片不重跑，重试不重复计数 |
| 数据剖析 | `app/pipeline.py` | 固定 7 阶段流水线，每阶段耗时与产物都可追溯 |
| 重复检测 | `app/duplicates.py` | 复合业务键重复与整行重复分开统计，输出重复组 |
| 异常检测 | `app/outliers.py` | 按品类分组，IQR(1.5×IQR) 与稳健 z(>3.5) 双法，样本 < 8 不判 |
| 评分卡 | `app/score.py` | 每列每规则 `1 − 违规率` 裁剪到 [0,1]，按权重归一为百分制 + 分级 |
| 工单导出 | `app/alerts.py` + `app/export.py` | 同一 `(row_id, column)` 多规则合并一条；工单与隔离记录分别导出 CSV |

页面 4 个：`/`（场景、演示步骤、规则集概览、当前发布版）、`/workbench`（样例/上传/粘贴 + 分片大小 +
分片进度/隔离/发布状态）、`/result/{run_id}`（作业视图或已发布结果：分数、违规率表、告警依据、样本不足、
隔离清单）、`/review`（历史、权重编辑与重算、工单/隔离下载）。

## 二、分片导入与发布口径（核心）

1. **记录级隔离**：表头契约错误（缺列、重复列名、空文件）仍直接 400；表头通过后逐条解析记录，
   坏记录进隔离区而不是静默跳过，原因码共 4 种：
   - `blank_line` 空白物理行；
   - `field_count_mismatch` 字段数与表头不一致（不按列名猜测填充）；
   - `missing_row_id` row_id 为空或占位符（无法定位的行不能进入剖析）；
   - `duplicate_row_id` row_id 与文件内更早记录重复（首次保留，后续隔离）。

   单元格层面的质量问题（数字列写「面议」、越界、缺失、异常值）是**健康记录上的告警**，不属于坏记录。
2. **分片检查点**：每片一个检查点，状态 `pending → processing → confirmed / failed`。
   确认是单事务：健康行写 `job_shards.rows_json`、坏记录写 `quarantine_records`，失败整体回滚，不留半片。
3. **失败续跑、不重复计数**：单片失败只标记该片，已确认片保留；`POST /api/runs/{id}/resume`
   把失败片退回 pending 重跑，**confirmed 片直接跳过**。每片有 `attempts` 计数可验证重试行为。
4. **发布闸门（核对恒等式）**：全部分片 confirmed 且
   `源记录数 = 健康记录数 + 隔离记录数`（同时核对分片记账与隔离表实际行数）才允许发布。
5. **原子发布、失败保留旧版**：核对通过后一次性把 `publish_status` 置为 `published` 并切换
   `settings.published_run_id` 指针；失败期间指针不动，线上分数、异常组与工单始终是上一版。
   未发布作业的工单接口返回 409。
6. **服务重启**：已确认分片与隔离记录持久化在 SQLite；进程内作业上下文（未确认片的源数据）释放后，
   续跑接口返回 409 中文提示，需重新上传新建作业。

## 三、质量规则

1. **缺失**：按列缺失率 `missing/total` 与规则 `max_missing_rate` 比较；`allowed`（未超允许，不扣分）/ `violation`（超了）/ `all_empty`（整列全空，最严重）。缺失值识别含 `NA`/`NULL`/`-` 等占位符。
2. **重复**：`business_key`（默认 `product_id + category`）重复 → `duplicate_key`，输出每组行号与额外行数；除 `row_id` 外所有列一致 → `duplicate_row`。整行重复整体归入 `duplicate_row`，不再重复计入 `duplicate_key`（避免同一批行双重扣分）。
3. **异常值**：按 `group_by`（品类）分组；组内有效样本 < `min_group_sample`(8) → `insufficient_sample`，整组不判。IQR 法（Q1−1.5·IQR / Q3+1.5·IQR，线性插值分位）与稳健 z（`|x−median|/(1.4826·MAD) > 3.5`）双法：**双命中 high、单命中 medium**；`MAD = 0` 时稳健 z 不可判（不误报，标注 `mad_zero`）。
4. **评分**：`score = 1 − 违规率` 裁剪到 `[0,1]`；单元权重 = 列权重 × 规则权重；`总分 = 100 × Σ(score×w) / Σw`；`A ≥ 90 / B ≥ 80 / C ≥ 70 / D < 70`。规则无有效样本时按满分计入并在结果页注明。
5. **告警去重**：同一 `(row_id, column)` 只保留一条，`rules[]` 为全部命中规则，`severity` 取最严重一档；缺失违规列按「前 20 行逐行 + 其余按列汇总」输出，避免单列 300 行把工单撑爆。

## 四、启动

```bash
cd projects/gb-630
docker compose config --quiet
docker compose up -d --build
docker compose ps                       # 等 healthy
curl -s http://127.0.0.1:18219/health
```

打开 <http://127.0.0.1:18219/>。数据落在命名卷 `qualiscan-data:/app/data`（SQLite `qualiscan.db`）。
首次启动幂等 seed 一次样例分片作业并发布（`settings.seed_version`，重复启动不产生脏数据；
v1 旧库的未发布 seed 会在升级时自动按新流程重建）。想重建库：`docker compose down -v` 后再 `up -d`。

容器内跑测试：

```bash
docker compose run --rm app python -m pytest -q
```

## 五、演示步骤

1. 首页看场景、规则集概览、当前已发布版本与样例埋点说明。
2. `/workbench` 选分片大小（默认 500 条/片，允许 50~5000），点「执行剖析（样例数据）」→
   页面按分片显示确认进度与「源 = 健康 + 隔离」计数 → 核对通过后自动跳结果页。
3. 粘贴一份含坏记录的 CSV（空白行、少字段、空 row_id、重复 row_id），观察隔离面板的原因码、
   行号与原始内容，并可下载隔离记录 CSV 退回上游修正。
4. 结果页：总分与分级、每列每规则违规率表、缺失明细、重复组、异常值分组统计、告警表（同格合并）、隔离 KPI。
5. `/review`：改列权重 → 保存 →「按当前权重重算当前发布版」→ 分数立刻变化；下载工单/隔离 CSV。
6. 错误态：上传缺列 CSV（例如删掉 `price`），返回 400 与中文原因，页面可修正后重试。

## 六、API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查（含运行数、规则版本、当前发布 run_id） |
| GET | `/api/rules` | 规则集原文 |
| POST | `/api/runs` | `{"source":"sample","shard_size":500}` 用样例执行，返回 `run_id` 与分片/隔离预算（后台线程跑） |
| POST | `/api/runs/upload?shard_size=` | multipart 上传 CSV（UTF-8/BOM/GB18030 自动识别） |
| POST | `/api/runs/text` | `{"csv_text": "...", "shard_size": 500}` 粘贴执行 |
| POST | `/api/runs/{id}/resume` | 失败作业从已确认处续跑；非失败态 409、上下文丢失 409 |
| GET | `/api/runs/{id}/status` | 分片进度（`shards[]`、`shard_counters`、三类计数、`publish_status`、`resumable`） |
| GET | `/api/runs/{id}/shards` | 全部分片检查点（状态/计数/尝试次数/失败原因/确认时间） |
| GET | `/api/runs/{id}/quarantine` | 隔离记录分页（原因分布 + 行号 + 原因说明 + 原始内容） |
| GET | `/api/runs/{id}/quarantine.csv` | 隔离记录 CSV（UTF-8 BOM，Excel 直接打开） |
| GET | `/api/runs/{id}/alerts.csv` | 工单 CSV（仅已发布作业，否则 409） |
| GET | `/api/published` | 当前线上版本摘要与工单/隔离/结果链接 |
| GET | `/api/runs` | 历史运行列表（含分片、三类计数、发布状态） |
| GET/POST | `/api/rules/weights` | 读取/保存权重覆盖（0~10，超范围 400） |
| POST | `/api/rules/weights/reset` | 恢复默认权重 |
| POST | `/api/runs/{id}/rescore` | 按当前权重重算已发布运行的总分 |

错误一律 4xx + `{"detail": "中文原因"}`：缺列、含未定义列、表头重复、空文件、只有表头、
超 8MB/2 万行、未知运行 404、未发布导出工单 409、非失败态续跑 409。

## 七、目录

```
app/
  main.py         路由（薄）
  settings.py     路径与常量（含分片大小上下限）
  loader.py       规则读取、CSV 编码探测、表头契约、记录级入口
  quarantine.py   坏记录隔离（4 类原因码 + 行号 + 原始内容）
  job.py          分片编排：确认/续跑/核对/发布、故障注入屏障
  store.py        SQLite（runs / job_shards / quarantine_records / settings，含 v1 迁移）
  missing.py      缺失规则
  duplicates.py   重复规则
  outliers.py     异常值规则（IQR + 稳健 z）
  validators.py   类型与范围校验
  score.py        评分卡
  alerts.py       告警合并去重
  export.py       工单 / 隔离记录 CSV
  pipeline.py     7 阶段流水线
  seed.py         幂等 seed（走完整分片发布链路）
  templates/      base/index/workbench/job/result/review
  static/         app.js / style.css / shell.css / theme.css
sample_data/      quality_sample.csv、rules.json、_generate.py（固定种子可重生成）
tests/            test_missing / test_duplicates / test_outliers / test_score / test_alerts /
                  test_api / test_quarantine / test_shards
scripts/          分片流程本地冒烟脚本（非 pytest 收集）
```

## 八、已知限制

- 评分是加权平均，A 级不等于没有问题：总分 93 时仍可能有上百个单元格命中规则，需按工单整改；分级与告警数是两个维度。
- 重复检测的整行签名按原始字符串比较，`0100` 与 `100`、`1.20` 与 `1.2` 视为不同；未做数值归一化（避免误合并）。
- 异常值以「品类」为唯一天然分组；不处理多列联合异常（如 price 与 weight 的比例异常）。
- MAD = 0 的组只做 IQR 判定；若 IQR 也为 0（全部取值相同），该组不会产生异常，仅记录 `mad_zero`。
- 未做登录/权限/审计，工单与隔离 CSV 直接下载；上传文件不落盘，只在内存中解析（≤8MB）。
- 后台计算为进程内线程，未引入队列；单实例部署（`docker compose` 一个服务）。未确认分片的源数据保存在
  进程内上下文（上限 32 个作业，超出淘汰已结束作业），服务重启后需重新上传才能续跑；已确认片与隔离记录不丢。
- 缺失违规列只逐行列出前 20 行，其余按列汇总；如需逐行完整清单请导出 CSV 后自行筛选。
