#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest_lab.precompute import (
    build_features,
    precompute_status,
    update_features,
    verify_features,
)
from backtest_lab.result_cache import prune_unreferenced


def main() -> int:
    parser = argparse.ArgumentParser(description="QuantWeave 公共日线特征预计算")
    parser.add_argument("command", choices=("build", "update", "verify", "status", "prune-cache"))
    parser.add_argument("--history-years", type=int)
    args = parser.parse_args()
    try:
        if args.command == "build":
            payload = build_features(history_years=args.history_years)
        elif args.command == "update":
            payload = update_features()
        elif args.command == "verify":
            payload = verify_features()
        elif args.command == "status":
            payload = precompute_status()
            payload["ok"] = payload.get("status") == "ready"
        else:
            payload = {"ok": True, **prune_unreferenced()}
        print(json.dumps(payload, ensure_ascii=False, default=str))
        return 0 if payload.get("ok") else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
