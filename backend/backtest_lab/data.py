from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import duckdb
import pandas as pd

from .config import MARKET_DB, MINUTE_DB, SAMPLE_MINUTES


MINUTE_SCHEMA = """
CREATE TABLE IF NOT EXISTS minute_bar (
    instrument_id VARCHAR NOT NULL,
    timestamp TIMESTAMP NOT NULL,
    frequency VARCHAR NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    volume DOUBLE,
    amount DOUBLE,
    source VARCHAR NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    PRIMARY KEY (instrument_id, timestamp, frequency)
);
CREATE TABLE IF NOT EXISTS minute_sync (
    instrument_id VARCHAR NOT NULL,
    frequency VARCHAR NOT NULL,
    min_timestamp TIMESTAMP,
    max_timestamp TIMESTAMP,
    rows BIGINT NOT NULL,
    source VARCHAR NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    error VARCHAR,
    PRIMARY KEY (instrument_id, frequency)
);
"""


@contextmanager
def market_connection():
    if not MARKET_DB.exists():
        raise FileNotFoundError(f"本地行情库不存在: {MARKET_DB}")
    connection = duckdb.connect(str(MARKET_DB), read_only=True)
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def minute_connection():
    MINUTE_DB.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(MINUTE_DB))
    connection.execute(MINUTE_SCHEMA)
    try:
        yield connection
    finally:
        connection.close()


def database_summary() -> dict:
    with market_connection() as connection:
        instruments = connection.execute(
            "SELECT count(*), count(*) FILTER (WHERE asset_type='stock') FROM instrument_master"
        ).fetchone()
        bars = connection.execute(
            "SELECT count(*), min(date), max(date), count(DISTINCT instrument_id) FROM daily_bar"
        ).fetchone()
    return {
        "instruments": int(instruments[0]),
        "stocks": int(instruments[1]),
        "daily_rows": int(bars[0]),
        "min_date": bars[1],
        "max_date": bars[2],
        "bar_instruments": int(bars[3]),
        "database": str(MARKET_DB),
    }


def resolve_codes(codes: Iterable[str]) -> list[dict]:
    cleaned = list(dict.fromkeys(str(code).strip().split(".")[0].zfill(6) for code in codes))
    cleaned = [code for code in cleaned if len(code) == 6 and code.isdigit()]
    if not cleaned:
        return []
    with market_connection() as connection:
        frame = connection.execute(
            """SELECT instrument_id, code, name, asset_type, exchange, market
               FROM instrument_master
               WHERE code IN (SELECT * FROM unnest(?))
               QUALIFY row_number() OVER (
                   PARTITION BY code ORDER BY asset_type='stock' DESC, asset_type='fund' DESC
               )=1""",
            [cleaned],
        ).df()
    mapping = {str(row.code): row for row in frame.itertuples(index=False)}
    return [
        {
            "instrument_id": str(mapping[code].instrument_id),
            "code": code,
            "name": str(mapping[code].name),
            "asset_type": str(mapping[code].asset_type),
            "exchange": str(mapping[code].exchange),
            "market": int(mapping[code].market),
        }
        for code in cleaned
        if code in mapping
    ]


def search_instruments(query: str, limit: int = 30) -> list[dict]:
    value = query.strip()
    if not value:
        return []
    with market_connection() as connection:
        rows = connection.execute(
            """SELECT code, name, asset_type, exchange, instrument_id
               FROM instrument_master
               WHERE asset_type IN ('stock','fund','index')
                 AND (code ILIKE ? OR name ILIKE ?)
               ORDER BY (code=?) DESC, (name=?) DESC, asset_type='stock' DESC
               LIMIT ?""",
            [f"%{value}%", f"%{value}%", value, value, int(limit)],
        ).fetchall()
    return [
        {"code": row[0], "name": row[1], "asset_type": row[2], "exchange": row[3], "instrument_id": row[4]}
        for row in rows
    ]


def load_daily(codes: Iterable[str], start: date | str, end: date | str) -> tuple[pd.DataFrame, list[dict]]:
    instruments = resolve_codes(codes)
    if not instruments:
        return pd.DataFrame(), []
    identifiers = [item["instrument_id"] for item in instruments]
    with market_connection() as connection:
        frame = connection.execute(
            """SELECT date, instrument_id, open, high, low, close, volume, amount
               FROM daily_bar
               WHERE instrument_id IN (SELECT * FROM unnest(?))
                 AND date BETWEEN ? AND ?
               ORDER BY date, instrument_id""",
            [identifiers, start, end],
        ).df()
    frame["date"] = pd.to_datetime(frame["date"])
    return frame, instruments


def load_execution_prices(
    instrument_ids: Iterable[str], start: date | str, end: date | str, frequency: str = "5m", entry_time: str = "14:45"
) -> pd.DataFrame:
    identifiers = list(dict.fromkeys(instrument_ids))
    if not identifiers or not MINUTE_DB.exists():
        return pd.DataFrame(columns=["date", "instrument_id", "execution_price", "bar_time", "amount"])
    with minute_connection() as connection:
        frame = connection.execute(
            """WITH candidates AS (
                   SELECT CAST(timestamp AS DATE) date, instrument_id, timestamp, open, close, amount,
                          row_number() OVER (
                              PARTITION BY CAST(timestamp AS DATE), instrument_id
                              ORDER BY CASE WHEN strftime(timestamp, '%H:%M') >= ? THEN 0 ELSE 1 END,
                                       abs(date_diff('minute', CAST(? AS TIME), CAST(timestamp AS TIME)))
                          ) rank
                   FROM minute_bar
                   WHERE frequency=?
                     AND instrument_id IN (SELECT * FROM unnest(?))
                     AND CAST(timestamp AS DATE) BETWEEN ? AND ?
                     AND strftime(timestamp, '%H:%M') BETWEEN '14:35' AND '14:55'
               )
               SELECT date, instrument_id, close execution_price, timestamp bar_time, amount
               FROM candidates WHERE rank=1 ORDER BY date, instrument_id""",
            [entry_time, entry_time, frequency, identifiers, start, end],
        ).df()
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"])
        frame["bar_time"] = pd.to_datetime(frame["bar_time"])
    return frame


def load_tail_window(
    instrument_ids: Iterable[str],
    start: date | str,
    end: date | str,
    frequency: str = "5m",
    decision_time: str = "14:45",
) -> pd.DataFrame:
    """Build point-in-time signal rows and the first executable bar after 14:45."""
    identifiers = list(dict.fromkeys(instrument_ids))
    if not identifiers or not MINUTE_DB.exists():
        return pd.DataFrame()
    with minute_connection() as connection:
        bars = connection.execute(
            """SELECT instrument_id,timestamp,open,high,low,close,volume,amount
               FROM minute_bar
               WHERE frequency=?
                 AND instrument_id IN (SELECT * FROM unnest(?))
                 AND CAST(timestamp AS DATE) BETWEEN ? AND ?
                 AND strftime(timestamp, '%H:%M') BETWEEN '14:30' AND '14:55'
               ORDER BY instrument_id,timestamp""",
            [frequency, identifiers, start, end],
        ).df()
    if bars.empty:
        return bars
    bars["timestamp"] = pd.to_datetime(bars["timestamp"])
    bars["date"] = bars["timestamp"].dt.normalize()
    bars["time"] = bars["timestamp"].dt.strftime("%H:%M")
    signal = bars[bars["time"] <= decision_time].copy()
    after = bars[bars["time"] > decision_time].copy()
    if signal.empty or after.empty:
        return pd.DataFrame()
    keys = ["date", "instrument_id"]
    signal_rows = (
        signal.sort_values("timestamp")
        .groupby(keys, as_index=False)
        .agg(
            signal_time=("timestamp", "max"),
            tail_open=("open", "first"),
            signal_price=("close", "last"),
            tail_high=("high", "max"),
            tail_low=("low", "min"),
            tail_volume=("volume", "sum"),
            tail_amount=("amount", "sum"),
        )
    )
    execution_rows = (
        after.sort_values("timestamp")
        .groupby(keys, as_index=False)
        .first()[keys + ["timestamp", "open", "high", "low", "close", "volume", "amount"]]
        .rename(
            columns={
                "timestamp": "execution_time",
                "open": "execution_price",
                "high": "execution_high",
                "low": "execution_low",
                "close": "execution_close",
                "volume": "execution_volume",
                "amount": "execution_amount",
            }
        )
    )
    result = signal_rows.merge(execution_rows, on=keys, how="inner")
    result["tail_return"] = result["signal_price"] / result["tail_open"] - 1
    return result.sort_values(["date", "instrument_id"])


def minute_coverage() -> pd.DataFrame:
    if not MINUTE_DB.exists():
        return pd.DataFrame(columns=["instrument_id", "frequency", "min_timestamp", "max_timestamp", "rows", "source"])
    with minute_connection() as connection:
        return connection.execute(
            "SELECT * FROM minute_sync ORDER BY max_timestamp DESC, instrument_id"
        ).df()


def write_minute_bars(frame: pd.DataFrame, source: str, frequency: str) -> int:
    if frame.empty:
        return 0
    required = {"instrument_id", "timestamp", "open", "high", "low", "close", "volume", "amount"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"分钟数据缺少字段: {sorted(missing)}")
    incoming = frame[list(required)].copy()
    incoming["timestamp"] = pd.to_datetime(incoming["timestamp"])
    incoming["frequency"] = frequency
    incoming["source"] = source
    incoming["updated_at"] = datetime.now().astimezone().replace(tzinfo=None)
    with minute_connection() as connection:
        connection.register("incoming_minute", incoming)
        connection.execute(
            """DELETE FROM minute_bar USING incoming_minute i
               WHERE minute_bar.instrument_id=i.instrument_id
                 AND minute_bar.timestamp=i.timestamp
                 AND minute_bar.frequency=i.frequency"""
        )
        connection.execute(
            """INSERT INTO minute_bar
               SELECT instrument_id,timestamp,frequency,open,high,low,close,volume,amount,source,updated_at
               FROM incoming_minute"""
        )
        connection.execute(
            """INSERT OR REPLACE INTO minute_sync
               SELECT instrument_id,frequency,min(timestamp),max(timestamp),count(*),any_value(source),max(updated_at),NULL
               FROM minute_bar
               WHERE instrument_id IN (SELECT DISTINCT instrument_id FROM incoming_minute)
                 AND frequency=?
               GROUP BY instrument_id,frequency""",
            [frequency],
        )
        connection.unregister("incoming_minute")
    return len(incoming)


def import_bundled_minutes() -> dict:
    if not SAMPLE_MINUTES.exists():
        return {"rows": 0, "message": "未找到随策略包保存的 15 分钟样本"}
    frame = pd.read_parquet(SAMPLE_MINUTES)
    suffix_map = {"SH": "SH", "SZ": "SZ", "BJ": "BJ"}
    parts = frame["ts_code"].astype(str).str.split(".", expand=True)
    frame["instrument_id"] = parts[1].map(suffix_map).fillna(parts[1]) + "." + parts[0].str.zfill(6)
    frame = frame.rename(columns={"trade_time": "timestamp", "vol": "volume"})
    rows = write_minute_bars(frame, "bundled-tushare-snapshot", "15m")
    return {"rows": rows, "codes": int(frame["instrument_id"].nunique()), "message": "15 分钟样本已导入"}
