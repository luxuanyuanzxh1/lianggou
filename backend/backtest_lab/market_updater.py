from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

import duckdb
import pandas as pd

from .config import MARKET_DB
from .minute_fetcher import _connect
from .precompute import market_snapshot, record_market_change, update_features


LEGACY_EOD_DB = MARKET_DB.with_name("a_share_eod.duckdb")
DAILY_CATEGORY = 9
BAR_COLUMNS = [
    "date",
    "instrument_id",
    "code",
    "asset_type",
    "open",
    "close",
    "high",
    "low",
    "volume",
    "amount",
]


@dataclass(frozen=True)
class UpdatePlan:
    target_date: date
    database_date: date | None
    eligible_instruments: int
    stale_instruments: int
    skipped_beijing: int


def _as_date(value: date | datetime | str | pd.Timestamp) -> date:
    return pd.Timestamp(value).date()


def load_update_plan(
    target_date: date | str | None = None,
    market_db: Path = MARKET_DB,
    limit: int | None = None,
) -> tuple[UpdatePlan, list[dict[str, Any]]]:
    """Return the exact local instruments that need an incremental refresh."""
    target = _as_date(target_date or date.today())
    if not market_db.is_file():
        raise FileNotFoundError(f"本地行情库不存在: {market_db}")
    with duckdb.connect(str(market_db), read_only=True) as connection:
        database_date = connection.execute("SELECT max(date) FROM daily_bar").fetchone()[0]
        skipped_beijing = connection.execute(
            "SELECT count(*) FROM instrument_master WHERE exchange='BJ'"
        ).fetchone()[0]
        rows = connection.execute(
            """SELECT i.instrument_id, i.market, i.code, i.name, i.asset_type,
                      i.exchange, max(d.date) AS max_date
               FROM instrument_master i
               LEFT JOIN daily_bar d USING (instrument_id)
               WHERE i.exchange IN ('SH', 'SZ')
                 AND i.asset_type IN ('stock', 'fund', 'index')
               GROUP BY i.instrument_id, i.market, i.code, i.name,
                        i.asset_type, i.exchange
               HAVING max(d.date) IS NULL OR max(d.date) < ?
               ORDER BY coalesce(max(d.date), DATE '1900-01-01'), i.instrument_id""",
            [target],
        ).fetchall()
        eligible = connection.execute(
            """SELECT count(*) FROM instrument_master
               WHERE exchange IN ('SH', 'SZ')
                 AND asset_type IN ('stock', 'fund', 'index')"""
        ).fetchone()[0]
    instruments = [
        {
            "instrument_id": row[0],
            "market": int(row[1]),
            "code": row[2],
            "name": row[3],
            "asset_type": row[4],
            "exchange": row[5],
            "max_date": row[6],
        }
        for row in (rows[:limit] if limit else rows)
    ]
    plan = UpdatePlan(
        target_date=target,
        database_date=database_date,
        eligible_instruments=int(eligible),
        stale_instruments=len(rows),
        skipped_beijing=int(skipped_beijing),
    )
    return plan, instruments


def normalize_daily_rows(
    rows: Iterable[dict[str, Any]],
    instrument: dict[str, Any],
    start_date: date,
    target_date: date,
) -> pd.DataFrame:
    frame = pd.DataFrame(list(rows))
    if frame.empty or "datetime" not in frame:
        return pd.DataFrame(columns=BAR_COLUMNS)
    frame = frame.rename(columns={"datetime": "date", "vol": "volume"})
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    frame = frame[(frame["date"] >= start_date) & (frame["date"] <= target_date)].copy()
    if frame.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)
    for column in ("open", "close", "high", "low", "volume", "amount"):
        frame[column] = pd.to_numeric(frame.get(column), errors="coerce")
    frame["instrument_id"] = instrument["instrument_id"]
    frame["code"] = instrument["code"]
    frame["asset_type"] = instrument["asset_type"]
    return (
        frame[BAR_COLUMNS]
        .dropna(subset=["date", "open", "close", "high", "low"])
        .drop_duplicates(["date", "instrument_id"], keep="last")
        .sort_values("date")
    )


def fetch_incremental_daily_bars(
    instruments: Iterable[dict[str, Any]],
    target_date: date,
    max_bars: int = 80,
    progress: Callable[[int, int, dict[str, Any]], None] | None = None,
) -> tuple[pd.DataFrame, list[dict[str, Any]], str]:
    """Fetch recent unadjusted bars from one persistent PyTDX connection."""
    selected = list(instruments)
    if not selected:
        return pd.DataFrame(columns=BAR_COLUMNS), [], "not-needed"
    api, host = _connect()
    frames: list[pd.DataFrame] = []
    results: list[dict[str, Any]] = []
    try:
        for index, instrument in enumerate(selected, start=1):
            last_date = instrument.get("max_date")
            start_date = _as_date(last_date) + timedelta(days=1) if last_date else date(1990, 1, 1)
            try:
                rows = api.get_security_bars(
                    DAILY_CATEGORY,
                    int(instrument["market"]),
                    str(instrument["code"]),
                    0,
                    max(1, min(int(max_bars), 800)),
                ) or []
                frame = normalize_daily_rows(rows, instrument, start_date, target_date)
                if not frame.empty:
                    frames.append(frame)
                item = {
                    "instrument_id": instrument["instrument_id"],
                    "code": instrument["code"],
                    "ok": True,
                    "rows": len(frame),
                }
            except Exception as exc:
                item = {
                    "instrument_id": instrument["instrument_id"],
                    "code": instrument["code"],
                    "ok": False,
                    "rows": 0,
                    "error": str(exc),
                }
            results.append(item)
            if progress:
                progress(index, len(selected), item)
    finally:
        api.disconnect()
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=BAR_COLUMNS)
    return combined, results, f"pytdx:{host}"


def _refresh_period_tables(connection: duckdb.DuckDBPyConnection, incoming: pd.DataFrame) -> None:
    if incoming.empty:
        return
    specs = {
        "weekly_bar": "date_trunc('week', date)",
        "monthly_bar": "date_trunc('month', date)",
        "quarterly_bar": "date_trunc('quarter', date)",
        "yearly_bar": "date_trunc('year', date)",
    }
    base_tables = {
        row[0]
        for row in connection.execute(
            """SELECT table_name
               FROM information_schema.tables
               WHERE table_schema='main' AND table_type='BASE TABLE'"""
        ).fetchall()
    }
    min_date = incoming["date"].min()
    identifiers = incoming["instrument_id"].drop_duplicates().tolist()
    for table, period_expression in specs.items():
        if table not in base_tables:
            continue
        min_period = connection.execute(
            f"SELECT CAST({period_expression} AS DATE) FROM (SELECT ? AS date)",
            [min_date],
        ).fetchone()[0]
        connection.execute(
            f"""DELETE FROM {table}
                WHERE instrument_id IN (SELECT * FROM unnest(?))
                  AND period_start >= ?""",
            [identifiers, min_period],
        )
        connection.execute(
            f"""INSERT INTO {table}
                SELECT CAST({period_expression} AS DATE) AS period_start,
                       instrument_id,
                       first(open ORDER BY date) AS open,
                       last(close ORDER BY date) AS close,
                       max(high) AS high,
                       min(low) AS low,
                       sum(volume) AS volume,
                       sum(amount) AS amount,
                       count(*) AS trading_days,
                       max(date) AS period_end
                FROM daily_bar
                WHERE instrument_id IN (SELECT * FROM unnest(?))
                  AND date >= ?
                GROUP BY 1, 2""",
            [identifiers, min_period],
        )


def _refresh_legacy_period_tables(
    connection: duckdb.DuckDBPyConnection,
    incoming: pd.DataFrame,
) -> None:
    if incoming.empty:
        return
    specs = {
        "weekly_bar": "date_trunc('week', date)",
        "monthly_bar": "date_trunc('month', date)",
        "quarterly_bar": "date_trunc('quarter', date)",
        "yearly_bar": "date_trunc('year', date)",
    }
    base_tables = {
        row[0]
        for row in connection.execute(
            """SELECT table_name
               FROM information_schema.tables
               WHERE table_schema='main' AND table_type='BASE TABLE'"""
        ).fetchall()
    }
    min_date = incoming["date"].min()
    codes = incoming["code"].drop_duplicates().tolist()
    for table, period_expression in specs.items():
        if table not in base_tables:
            continue
        min_period = connection.execute(
            f"SELECT CAST({period_expression} AS DATE) FROM (SELECT ? AS date)",
            [min_date],
        ).fetchone()[0]
        connection.execute(
            f"""DELETE FROM {table}
                WHERE code IN (SELECT * FROM unnest(?))
                  AND period_start >= ?""",
            [codes, min_period],
        )
        connection.execute(
            f"""INSERT INTO {table}
                SELECT CAST({period_expression} AS DATE) AS period_start,
                       code,
                       first(open ORDER BY date) AS open,
                       last(close ORDER BY date) AS close,
                       max(high) AS high,
                       min(low) AS low,
                       sum(volume) AS volume,
                       sum(amount) AS amount,
                       count(*) AS trading_days,
                       max(date) AS period_end
                FROM daily_bar
                WHERE code IN (SELECT * FROM unnest(?))
                  AND date >= ?
                GROUP BY 1, 2""",
            [codes, min_period],
        )


def write_market_database(frame: pd.DataFrame, market_db: Path = MARKET_DB) -> int:
    if frame.empty:
        return 0
    incoming = frame[BAR_COLUMNS].copy()
    incoming["date"] = pd.to_datetime(incoming["date"]).dt.date
    with duckdb.connect(str(market_db)) as connection:
        connection.register("incoming_daily", incoming)
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                """DELETE FROM daily_bar USING incoming_daily i
                   WHERE daily_bar.date=i.date
                     AND daily_bar.instrument_id=i.instrument_id"""
            )
            connection.execute(
                """INSERT INTO daily_bar
                   SELECT date,instrument_id,open,close,high,low,volume,amount
                   FROM incoming_daily"""
            )
            connection.execute(
                """INSERT OR REPLACE INTO sync_state
                   SELECT d.instrument_id, 'ok', count(*), min(d.date), max(d.date), ?, NULL
                   FROM daily_bar d
                   WHERE d.instrument_id IN (SELECT DISTINCT instrument_id FROM incoming_daily)
                   GROUP BY d.instrument_id""",
                [datetime.now().astimezone().replace(tzinfo=None)],
            )
            _refresh_period_tables(connection, incoming)
            record_market_change(connection, incoming)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.unregister("incoming_daily")
    return len(incoming)


def write_legacy_eod_database(
    frame: pd.DataFrame,
    source: str,
    legacy_db: Path = LEGACY_EOD_DB,
) -> int:
    stocks = frame[frame["asset_type"] == "stock"].copy()
    if stocks.empty or not legacy_db.is_file():
        return 0
    columns = ["date", "code", "open", "close", "high", "low", "volume", "amount"]
    incoming = stocks[columns].copy()
    incoming["date"] = pd.to_datetime(incoming["date"]).dt.date
    with duckdb.connect(str(legacy_db)) as connection:
        connection.register("incoming_eod", incoming)
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                """DELETE FROM daily_bar USING incoming_eod i
                   WHERE daily_bar.date=i.date AND daily_bar.code=i.code"""
            )
            connection.execute("INSERT INTO daily_bar SELECT * FROM incoming_eod")
            _refresh_legacy_period_tables(connection, incoming)
            connection.execute(
                "INSERT INTO update_log VALUES (?, 'daily', ?, ?, ?)",
                [
                    datetime.now().astimezone().replace(tzinfo=None),
                    source,
                    len(incoming),
                    "standalone incremental scheduler",
                ],
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.unregister("incoming_eod")
    return len(incoming)


def run_market_update(
    target_date: date | str | None = None,
    market_db: Path = MARKET_DB,
    legacy_db: Path = LEGACY_EOD_DB,
    max_bars: int = 80,
    limit: int | None = None,
    write_legacy: bool = True,
    update_precomputed: bool = True,
    progress: Callable[[int, int, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    started_at = datetime.now().astimezone()
    plan, instruments = load_update_plan(target_date, market_db, limit)
    bars, details, source = fetch_incremental_daily_bars(
        instruments,
        plan.target_date,
        max_bars=max_bars,
        progress=progress,
    )
    market_rows = write_market_database(bars, market_db)
    legacy_rows = write_legacy_eod_database(bars, source, legacy_db) if write_legacy else 0
    precompute_result: dict[str, Any] | None = None
    precompute_error: str | None = None
    if update_precomputed:
        try:
            precompute_result = update_features(market_db=market_db)
        except Exception as exc:
            # Market data is already committed. A stale feature store is safe:
            # the backtest planner will fall back to the original dynamic SQL.
            precompute_error = str(exc)
    failures = [item for item in details if not item.get("ok")]
    snapshot = market_snapshot(market_db)
    return {
        "ok": not failures,
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "target_date": plan.target_date.isoformat(),
        "database_date_before": plan.database_date.isoformat() if plan.database_date else None,
        "eligible_instruments": plan.eligible_instruments,
        "planned_instruments": len(instruments),
        "stale_instruments": plan.stale_instruments,
        "skipped_beijing": plan.skipped_beijing,
        "market_rows_written": market_rows,
        "legacy_rows_written": legacy_rows,
        "dataset_version": snapshot.get("dataset_version"),
        "precompute_enabled": update_precomputed,
        "precompute_ok": precompute_error is None if update_precomputed else None,
        "precompute": precompute_result,
        "precompute_error": precompute_error,
        "failed_instruments": len(failures),
        "failure_samples": failures[:20],
        "source": source,
    }
