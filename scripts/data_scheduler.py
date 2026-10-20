#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest_lab.platform_lock import FileLock

STATE_PATH = ROOT / "work" / "data-scheduler" / "status.json"
LOCK_PATH = ROOT / "work" / "data-scheduler.lock"
LOG_PATH = ROOT / "work" / "data-scheduler" / "runs.log"


def parse_clock(value: str) -> clock_time:
    try:
        hour, minute = (int(part) for part in value.split(":"))
        return clock_time(hour=hour, minute=minute)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("时间格式应为 HH:MM，例如 18:10") from exc


def read_state() -> dict:
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(payload: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_PATH)


def is_due(now: datetime, scheduled_at: clock_time, state: dict, force: bool = False) -> bool:
    if force:
        return True
    if now.weekday() >= 5 or now.time() < scheduled_at:
        return False
    last_success = str(state.get("last_success_date") or "")
    if last_success == now.date().isoformat():
        return False
    next_retry = state.get("next_retry_at")
    if next_retry:
        try:
            if now < datetime.fromisoformat(str(next_retry)):
                return False
        except ValueError:
            pass
    return True


def next_scheduled_at(now: datetime, scheduled_at: clock_time) -> datetime:
    candidate = datetime.combine(now.date(), scheduled_at, tzinfo=now.tzinfo)
    if now.weekday() < 5 and now < candidate:
        return candidate
    candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def mark_waiting(scheduled_at: clock_time, started_at: str | None = None) -> None:
    now = datetime.now().astimezone()
    state = read_state()
    state.update({
        "status": "waiting",
        "pid": os.getpid(),
        "daemon_started_at": started_at or str(state.get("daemon_started_at") or now.isoformat(timespec="seconds")),
        "last_seen_at": now.isoformat(timespec="seconds"),
        "schedule": scheduled_at.isoformat(timespec="minutes"),
        "next_scheduled_at": next_scheduled_at(now, scheduled_at).isoformat(timespec="seconds"),
    })
    write_state(state)


def append_log(payload: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def update_command(update_precomputed: bool = True) -> list[str]:
    command = [sys.executable, str(ROOT / "scripts" / "update_market_data.py")]
    if not update_precomputed:
        command.append("--no-precompute")
    return command


def run_update(
    retry_minutes: int,
    scheduled_at: clock_time,
    update_precomputed: bool = True,
) -> dict:
    started = datetime.now().astimezone()
    state = read_state()
    state.update({
        "status": "running",
        "pid": os.getpid(),
        "last_started_at": started.isoformat(timespec="seconds"),
        "schedule": scheduled_at.isoformat(timespec="minutes"),
    })
    write_state(state)
    command = update_command(update_precomputed)
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    output_lines = (result.stdout or "").strip().splitlines()
    try:
        detail = json.loads(output_lines[-1]) if output_lines else {}
    except json.JSONDecodeError:
        detail = {"output": (result.stdout or "")[-2000:]}
    finished = datetime.now().astimezone()
    ok = result.returncode == 0 and bool(detail.get("ok"))
    state.update({
        "status": "ready" if ok else "error",
        "last_finished_at": finished.isoformat(timespec="seconds"),
        "last_result": detail,
        "last_error": None if ok else ((result.stderr or "").strip()[-2000:] or detail.get("error")),
        "next_retry_at": None if ok else (finished + timedelta(minutes=retry_minutes)).isoformat(timespec="seconds"),
        "next_scheduled_at": next_scheduled_at(finished, scheduled_at).isoformat(timespec="seconds"),
    })
    if ok:
        state["last_success_date"] = finished.date().isoformat()
    write_state(state)
    append_log(state)
    return state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="睿测本地数据更新调度器")
    parser.add_argument(
        "--time",
        type=parse_clock,
        default=parse_clock(os.environ.get("RUIPAN_DATA_UPDATE_TIME", "18:10")),
        help="工作日执行时间，默认 18:10",
    )
    parser.add_argument("--once", action="store_true", help="立即执行一次后退出")
    parser.add_argument("--no-precompute", action="store_true", help="只更新行情，预计算由独立任务执行")
    parser.add_argument("--dry-run", action="store_true", help="只显示是否到期，不抓取或写库")
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--retry-minutes", type=int, default=30)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_context = FileLock(LOCK_PATH)
        lock_context.__enter__()
    except (BlockingIOError, OSError):
        print("数据调度器已经在运行", file=sys.stderr)
        return 2
    try:
        daemon_started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        if args.dry_run:
            now = datetime.now().astimezone()
            print(json.dumps({
                "ok": True,
                "dry_run": True,
                "now": now.isoformat(timespec="seconds"),
                "scheduled_time": args.time.isoformat(timespec="minutes"),
                "due": is_due(now, args.time, read_state(), force=args.once),
            }, ensure_ascii=False))
            return 0
        if args.once:
            state = run_update(args.retry_minutes, args.time, update_precomputed=not args.no_precompute)
            print(json.dumps(state, ensure_ascii=False, default=str))
            return 0 if state.get("status") == "ready" else 1
        mark_waiting(args.time, daemon_started_at)
        while True:
            now = datetime.now().astimezone()
            if is_due(now, args.time, read_state()):
                run_update(args.retry_minutes, args.time, update_precomputed=not args.no_precompute)
            else:
                mark_waiting(args.time, daemon_started_at)
            time.sleep(max(5, args.poll_seconds))
    finally:
        lock_context.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())
