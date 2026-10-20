#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest_lab.market_updater import load_update_plan, run_market_update
from backtest_lab.platform_lock import FileLock


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="增量更新睿测本地日线数据库")
    parser.add_argument("--target-date", default=date.today().isoformat())
    parser.add_argument("--max-bars", type=int, default=80)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-legacy", action="store_true")
    parser.add_argument("--no-precompute", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    lock_path = ROOT / "work" / "market-data-update.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_context = FileLock(lock_path)
        lock_context.__enter__()
    except (BlockingIOError, OSError):
        print(json.dumps({"ok": False, "error": "已有数据更新任务正在运行"}, ensure_ascii=False))
        return 2
    try:
        if args.dry_run:
            plan, instruments = load_update_plan(args.target_date, limit=args.limit)
            payload = {
                "ok": True,
                "dry_run": True,
                "plan": asdict(plan),
                "selected_instruments": len(instruments),
                "sample": instruments[:10],
            }
        else:
            def progress(index: int, total: int, item: dict) -> None:
                if index == 1 or index == total or index % 100 == 0:
                    print(
                        f"[{index}/{total}] {item.get('code')} rows={item.get('rows', 0)} "
                        f"ok={item.get('ok')}",
                        file=sys.stderr,
                        flush=True,
                    )

            payload = run_market_update(
                args.target_date,
                max_bars=args.max_bars,
                limit=args.limit,
                write_legacy=not args.no_legacy,
                update_precomputed=not args.no_precompute,
                progress=progress,
            )
        print(json.dumps(payload, ensure_ascii=False, default=str))
        return 0 if payload.get("ok") else 1
    finally:
        lock_context.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())
