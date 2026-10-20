from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

import pandas as pd
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData

from .data import load_daily, minute_connection, resolve_codes


CHINA_TZ = ZoneInfo("Asia/Shanghai")
EXCHANGES = {"SH": Exchange.SSE, "SZ": Exchange.SZSE, "BJ": Exchange.BSE}


def framework_status() -> dict:
    import vnpy
    import vnpy_portfoliostrategy

    try:
        import vnpy.alpha  # noqa: F401
        alpha_ready = True
    except Exception:
        alpha_ready = False
    return {
        "framework": "VeighNa",
        "vnpy": vnpy.__version__,
        "portfolio_strategy": vnpy_portfoliostrategy.__version__,
        "alpha_ready": alpha_ready,
        "gateway": "RUIPAN_DUCKDB",
    }


def load_daily_bars(codes: list[str], start: date | str, end: date | str) -> dict[str, list[BarData]]:
    frame, instruments = load_daily(codes, start, end)
    metadata = {item["instrument_id"]: item for item in instruments}
    output: dict[str, list[BarData]] = {}
    for instrument_id, part in frame.groupby("instrument_id"):
        item = metadata[str(instrument_id)]
        exchange = EXCHANGES[item["exchange"]]
        bars = []
        for row in part.sort_values("date").itertuples(index=False):
            timestamp = pd.Timestamp(row.date).replace(hour=15).to_pydatetime().replace(tzinfo=CHINA_TZ)
            bars.append(
                BarData(
                    gateway_name="RUIPAN_DUCKDB",
                    symbol=item["code"],
                    exchange=exchange,
                    datetime=timestamp,
                    interval=Interval.DAILY,
                    volume=float(row.volume or 0),
                    turnover=float(row.amount or 0),
                    open_price=float(row.open),
                    high_price=float(row.high),
                    low_price=float(row.low),
                    close_price=float(row.close),
                )
            )
        output[f"{item['code']}.{exchange.value}"] = bars
    return output


def load_minute_bars(
    codes: list[str], start: date | str, end: date | str, frequency: str = "5m"
) -> dict[str, list[BarData]]:
    instruments = resolve_codes(codes)
    identifiers = [item["instrument_id"] for item in instruments]
    metadata = {item["instrument_id"]: item for item in instruments}
    if not identifiers:
        return {}
    with minute_connection() as connection:
        frame = connection.execute(
            """SELECT instrument_id,timestamp,open,high,low,close,volume,amount
               FROM minute_bar
               WHERE instrument_id IN (SELECT * FROM unnest(?))
                 AND frequency=? AND CAST(timestamp AS DATE) BETWEEN ? AND ?
               ORDER BY timestamp,instrument_id""",
            [identifiers, frequency, start, end],
        ).df()
    output: dict[str, list[BarData]] = {}
    for instrument_id, part in frame.groupby("instrument_id"):
        item = metadata[str(instrument_id)]
        exchange = EXCHANGES[item["exchange"]]
        bars = []
        for row in part.itertuples(index=False):
            timestamp = pd.Timestamp(row.timestamp).to_pydatetime().replace(tzinfo=CHINA_TZ)
            bars.append(
                BarData(
                    gateway_name="RUIPAN_DUCKDB",
                    symbol=item["code"],
                    exchange=exchange,
                    datetime=timestamp,
                    interval=Interval.MINUTE,
                    volume=float(row.volume or 0),
                    turnover=float(row.amount or 0),
                    open_price=float(row.open),
                    high_price=float(row.high),
                    low_price=float(row.low),
                    close_price=float(row.close),
                )
            )
        output[f"{item['code']}.{exchange.value}"] = bars
    return output


def smoke_test() -> dict:
    status = framework_status()
    daily = load_daily_bars(["600519"], "2026-01-01", "2026-07-14")
    minute = load_minute_bars(["600519"], "2026-07-01", "2026-07-14", "5m")
    status.update(
        {
            "daily_symbols": len(daily),
            "daily_bars": sum(map(len, daily.values())),
            "minute_symbols": len(minute),
            "minute_bars": sum(map(len, minute.values())),
        }
    )
    return status
