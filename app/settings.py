"""运行参数：路径与常量（容器内数据目录由环境变量覆盖）。"""
from __future__ import annotations

import os
import pathlib

APP_NAME = "抽检质量评分台"
APP_SHORT = "qualiscan"

BASE_DIR = pathlib.Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent

DATA_DIR = pathlib.Path(os.environ.get("QUALISCAN_DATA_DIR", PROJECT_DIR / "data"))
SAMPLE_DIR = pathlib.Path(os.environ.get("QUALISCAN_SAMPLE_DIR", PROJECT_DIR / "sample_data"))
DB_PATH = DATA_DIR / "qualiscan.db"
SAMPLE_CSV = SAMPLE_DIR / "quality_sample.csv"
RULES_JSON = SAMPLE_DIR / "rules.json"

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_ROWS = 20000
SEED_VERSION = "2"

# 分片导入：每片默认 500 条记录；API 允许在 [50, 5000] 之间覆盖
DEFAULT_SHARD_SIZE = 500
MIN_SHARD_SIZE = 50
MAX_SHARD_SIZE = 5000
