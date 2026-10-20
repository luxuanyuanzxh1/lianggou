#!/usr/bin/env python3
"""Run the offline startup check and repair generated web catalogs."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest_lab.platform_lock import FileLock

DATA = ROOT / "public" / "data"
DB = ROOT / "data" / "market_data.duckdb"
OUTPUT = DATA / "quantweave-startup-check.json"
LOCK = ROOT / "work" / "workspace-check.lock"


def load_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def run_check() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    checks: list[dict] = []

    if not DB.is_file() or DB.stat().st_size == 0:
        checks.append({"id": "duckdb", "status": "error", "detail": "本地 market_data.duckdb 不存在"})
    else:
        checks.append({"id": "duckdb", "status": "ok", "detail": str(DB)})

    required = [("market_status", "market-data-status.json")]
    for check_id, filename in required:
        payload = load_json(DATA / filename)
        checks.append({
            "id": check_id,
            "status": "ok" if isinstance(payload, dict) and payload else "error",
            "detail": filename if payload else "文件缺失或格式错误",
        })

    status = "ready" if all(item["status"] == "ok" for item in checks) else "warning"
    payload = {"status": status, "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
               "mode": "quantweave-backtest", "repairs": [], "checks": checks}
    temporary = OUTPUT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(OUTPUT)
    print(json.dumps(payload, ensure_ascii=False))


def main() -> None:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(LOCK):
        run_check()


if __name__ == "__main__":
    main()
