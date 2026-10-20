from __future__ import annotations

from datetime import date, datetime
from typing import Callable, Iterable

import pandas as pd

from .config import TDX_HOSTS
from .data import resolve_codes, write_minute_bars


CATEGORY = {"1m": 8, "5m": 0, "15m": 1}


def _connect():
    try:
        from pytdx.hq import TdxHq_API
    except ImportError as exc:
        raise RuntimeError("缺少 pytdx，请先运行 npm run backtest:setup") from exc
    errors: list[str] = []
    for host, port in TDX_HOSTS:
        api = TdxHq_API(heartbeat=True, auto_retry=True, raise_exception=True)
        try:
            if api.connect(host, port):
                return api, host
        except Exception as exc:
            errors.append(f"{host}: {exc}")
    raise ConnectionError("通达信行情节点均不可用: " + "; ".join(errors))


def fetch_one(
    code: str,
    start: date | str,
    end: date | str,
    frequency: str = "5m",
    max_bars: int = 24_000,
    window_only: bool = True,
) -> dict:
    if frequency not in CATEGORY:
        raise ValueError(f"不支持的分钟周期: {frequency}")
    resolved = resolve_codes([code])
    if not resolved:
        raise ValueError(f"本地证券目录没有 {code}")
    instrument = resolved[0]
    if instrument["exchange"] == "BJ":
        raise ValueError("当前通达信公共节点不保证北交所分钟数据完整")
    market = 1 if instrument["exchange"] == "SH" else 0
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    api, host = _connect()
    rows: list[dict] = []
    try:
        for offset in range(0, max_bars, 800):
            batch = api.get_security_bars(CATEGORY[frequency], market, instrument["code"], offset, 800) or []
            if not batch:
                break
            rows.extend(dict(item) for item in batch)
            oldest = pd.Timestamp(batch[0]["datetime"])
            if oldest <= start_ts:
                break
    finally:
        api.disconnect()
    if not rows:
        return {"code": code, "name": instrument["name"], "rows": 0, "source": f"pytdx:{host}"}
    frame = pd.DataFrame(rows).rename(columns={"datetime": "timestamp", "vol": "volume"})
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    frame = frame[(frame["timestamp"] >= start_ts) & (frame["timestamp"] <= end_ts)]
    if window_only:
        times = frame["timestamp"].dt.strftime("%H:%M")
        frame = frame[(times >= "14:30") & (times <= "15:00")]
    frame["instrument_id"] = instrument["instrument_id"]
    frame = frame.drop_duplicates(["instrument_id", "timestamp"]).sort_values("timestamp")
    written = write_minute_bars(frame, f"pytdx:{host}", frequency)
    return {
        "code": instrument["code"],
        "name": instrument["name"],
        "rows": written,
        "min_timestamp": frame["timestamp"].min() if written else None,
        "max_timestamp": frame["timestamp"].max() if written else None,
        "source": f"pytdx:{host}",
    }


def fetch_many(
    codes: Iterable[str],
    start: date | str,
    end: date | str,
    frequency: str = "5m",
    max_bars: int = 24_000,
    progress: Callable[[int, int, dict], None] | None = None,
) -> list[dict]:
    cleaned = list(dict.fromkeys(str(code).strip().split(".")[0].zfill(6) for code in codes))
    results: list[dict] = []
    total = len(cleaned)
    for index, code in enumerate(cleaned, start=1):
        try:
            result = fetch_one(code, start, end, frequency, max_bars=max_bars)
            result["ok"] = True
        except Exception as exc:
            result = {"code": code, "rows": 0, "ok": False, "error": str(exc)}
        results.append(result)
        if progress:
            progress(index, total, result)
    return results

