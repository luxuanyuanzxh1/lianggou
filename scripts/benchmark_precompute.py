#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gc
import json
import statistics
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest_lab.lego_pipeline import run_lego_backtest, standard_multifactor_pipeline
from backtest_lab.precompute import market_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="三年标准多因子预计算性能基准")
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    maximum = market_snapshot().get("max_trade_date")
    if maximum is None:
        raise SystemExit("行情库没有可用日期")
    start = maximum - timedelta(days=365 * 3)
    pipeline = replace(
        standard_multifactor_pipeline(),
        start=start.isoformat(),
        end=maximum.isoformat(),
    )
    timings = []
    rows = []
    for _ in range(max(1, args.runs)):
        gc.collect()
        result = run_lego_backtest(pipeline, persist=False)
        timings.append(float(result["timings"]["total_seconds"]))
        rows.append(result["timings"])
        del result
    payload = {
        "ok": statistics.median(timings) <= 10.0,
        "runs": len(timings),
        "median_seconds": statistics.median(timings),
        "max_seconds": max(timings),
        "target_seconds": 10.0,
        "details": rows,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
