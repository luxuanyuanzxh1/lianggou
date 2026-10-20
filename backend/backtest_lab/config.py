from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# 数据目录可切换：QW_DATA_DIR 指向「本地主库桥接目录」即可整体换源，
# 不设置时保持原行为（服务器拉回的数据副本）。
DATA_DIR = Path(os.environ.get("QW_DATA_DIR") or (ROOT / "data")).expanduser().resolve()
MARKET_DB = DATA_DIR / "market_data.duckdb"
MINUTE_DB = DATA_DIR / "minute_bars.duckdb"
FEATURE_DB = DATA_DIR / "feature_store.duckdb"
PRODUCT_WORK_DIR = ROOT / "work" / "quantweave"
RUNS_DIR = PRODUCT_WORK_DIR / "runs"
RESULT_CACHE_DIR = PRODUCT_WORK_DIR / "cache" / "results"
PRECOMPUTE_WORK_DIR = ROOT / "work" / "precompute"
SAMPLE_MINUTES = ROOT / "work" / "sample-minutes-15m.parquet"

DEFAULT_CODES = ["600519", "000858", "300750", "601318", "600036"]
TDX_HOSTS = [
    ("shtdx.gtjas.com", 7709),
    ("sztdx.gtjas.com", 7709),
    ("jstdx.gtjas.com", 7709),
    ("115.238.56.198", 7709),
    ("180.153.18.170", 7709),
]
