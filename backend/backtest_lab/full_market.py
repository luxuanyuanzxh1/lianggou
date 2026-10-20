"""Vectorized full-market A-share portfolio backtester.

DuckDB calculates point-in-time indicators and the daily cross-sectional Top N
inside the database.  Python only replays the small candidate union, which keeps
the full-market path usable without creating thousands of Backtrader feeds.

Signals are frozen after the daily close and orders are executed at the next
complete market day's open.  The execution model is deliberately simulation
only and contains no broker/order gateway code.
"""

from __future__ import annotations

import math
import os
import uuid
from collections.abc import Iterable
from dataclasses import asdict
from datetime import datetime
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from .config import MARKET_DB
from .engine import BacktestConfig, _metrics, persist_result
from .strategies import STRATEGIES


SUPPORTED_STRATEGIES = {"buy_hold", "ma_cross", "rsi", "momentum", "turtle"}
VALID_SCOPES = {"all", "sh_sz"}
ACTIVE_GATE_EXIT_REASON = "活跃市值门控关闭强制退出"
ACTIVE_GATE_RETRY_REASON = "活跃市值门控关闭未成交逐日重试"
DEFAULT_MAX_POSITION_PCT = 0.10


def _scope_clause(scope: str) -> str:
    if scope == "all":
        return "m.exchange IN ('SH', 'SZ', 'BJ')"
    if scope == "sh_sz":
        return "m.exchange IN ('SH', 'SZ')"
    raise ValueError("scope 只能是 all 或 sh_sz")


def _limit_rate(code: str, exchange: str, name: str = "") -> float:
    """Daily price-limit ratio for ST and the supported A-share boards."""
    if "ST" in str(name or "").upper():
        return 0.05
    if exchange == "BJ" or code.startswith(("4", "8", "92")):
        return 0.30
    if code.startswith(("300", "301", "688")):
        return 0.20
    return 0.10


def _fee(config: BacktestConfig, side: str, amount: float) -> float:
    if amount <= 0:
        return 0.0
    value = max(float(config.min_commission), amount * float(config.commission))
    value += amount * float(config.transfer_fee)
    if side == "卖出":
        value += amount * float(config.stamp_tax)
    return float(value)


def _floor_lot(shares: float, lot_size: int) -> int:
    if not math.isfinite(float(shares)) or shares <= 0:
        return 0
    return max(0, int(float(shares) // lot_size) * lot_size)


def _complete_dates_sql(scope_clause: str) -> str:
    """CTEs shared by the vectorized queries.

    A date is dropped when its stock coverage is below 80% of the preceding
    20-session median.  This is important for partially synchronized database
    tails (for example 2026-07-15 currently contains only 24 stocks).  The
    rolling comparison adapts to the historical number of listed stocks.
    """
    return f"""
    universe_raw AS (
        SELECT d.date, d.instrument_id, m.code, m.name, m.exchange,
               d.open, d.high, d.low, d.close, d.volume, d.amount
        FROM daily_bar d
        JOIN instrument_master m USING (instrument_id)
        WHERE m.asset_type = 'stock'
          AND {scope_clause}
          AND regexp_matches(coalesce(m.code, ''), '^[0-9]{{6}}$')
          AND upper(coalesce(m.name, '')) NOT LIKE '%ST%'
          AND d.date BETWEEN ? AND ?
          AND d.open > 0 AND d.high > 0 AND d.low > 0 AND d.close > 0
    ),
    daily_counts AS (
        SELECT date, count(DISTINCT instrument_id) AS stock_count
        FROM universe_raw
        GROUP BY date
    ),
    coverage AS (
        SELECT date, stock_count,
               median(stock_count) OVER (
                   ORDER BY date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
               ) AS prior_20_median
        FROM daily_counts
    ),
    complete_dates AS (
        SELECT date, stock_count, prior_20_median
        FROM coverage
        WHERE prior_20_median IS NULL
           OR stock_count >= greatest(1, prior_20_median * 0.80)
    )
    """


def _strategy_expressions(strategy: str, params: dict[str, Any]) -> tuple[str, str]:
    if strategy == "buy_hold":
        return "rn >= 20", "liquidity20"
    if strategy == "ma_cross":
        slow = max(2, int(params.get("slow", 20)))
        return f"rn >= {slow} AND ma_fast > ma_slow", "ma_fast / nullif(ma_slow, 0) - 1"
    if strategy == "rsi":
        return (
            "last_rsi_entry IS NOT NULL AND "
            "(last_rsi_exit IS NULL OR last_rsi_entry > last_rsi_exit)",
            "-rsi_value",
        )
    if strategy == "momentum":
        lookback = max(1, int(params.get("lookback", 20)))
        return f"rn > {lookback} AND momentum_value > 0", "momentum_value"
    if strategy == "turtle":
        return (
            "last_turtle_entry IS NOT NULL AND "
            "(last_turtle_exit IS NULL OR last_turtle_entry > last_turtle_exit)",
            "close / nullif(turtle_entry_level, 0) - 1",
        )
    raise ValueError(f"全市场模式暂不支持策略: {strategy}")


def _candidate_query(
    scope_clause: str,
    strategy: str,
    params: dict[str, Any],
    top_n: int,
) -> str:
    fast = max(1, int(params.get("fast", 5)))
    slow = max(fast + 1, int(params.get("slow", 20)))
    rsi_period = max(2, int(params.get("period", 14)))
    rsi_buy = float(params.get("buy_level", 30))
    rsi_sell = float(params.get("sell_level", 70))
    momentum_lookback = max(1, int(params.get("lookback", 20)))
    turtle_entry = max(2, int(params.get("entry", 20)))
    turtle_exit = max(2, int(params.get("exit", 10)))
    eligible, score = _strategy_expressions(strategy, params)
    return f"""
    WITH {_complete_dates_sql(scope_clause)},
    base AS (
        SELECT r.*,
               row_number() OVER stock_window AS rn,
               lag(close) OVER stock_window AS prev_close,
               close - lag(close) OVER stock_window AS delta,
               avg(close) OVER (
                   PARTITION BY instrument_id ORDER BY date
                   ROWS BETWEEN {fast - 1} PRECEDING AND CURRENT ROW
               ) AS ma_fast,
               avg(close) OVER (
                   PARTITION BY instrument_id ORDER BY date
                   ROWS BETWEEN {slow - 1} PRECEDING AND CURRENT ROW
               ) AS ma_slow,
               lag(close, {momentum_lookback}) OVER stock_window AS momentum_base,
               max(high) OVER (
                   PARTITION BY instrument_id ORDER BY date
                   ROWS BETWEEN {turtle_entry} PRECEDING AND 1 PRECEDING
               ) AS turtle_entry_level,
               min(low) OVER (
                   PARTITION BY instrument_id ORDER BY date
                   ROWS BETWEEN {turtle_exit} PRECEDING AND 1 PRECEDING
               ) AS turtle_exit_level,
               avg(coalesce(amount, 0)) OVER (
                   PARTITION BY instrument_id ORDER BY date
                   ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
               ) AS liquidity20
        FROM universe_raw r
        JOIN complete_dates USING (date)
        WINDOW stock_window AS (PARTITION BY instrument_id ORDER BY date)
    ),
    indicators AS (
        SELECT *,
               avg(greatest(delta, 0)) OVER (
                   PARTITION BY instrument_id ORDER BY date
                   ROWS BETWEEN {rsi_period - 1} PRECEDING AND CURRENT ROW
               ) AS avg_gain,
               avg(greatest(-delta, 0)) OVER (
                   PARTITION BY instrument_id ORDER BY date
                   ROWS BETWEEN {rsi_period - 1} PRECEDING AND CURRENT ROW
               ) AS avg_loss,
               close / nullif(momentum_base, 0) - 1 AS momentum_value
        FROM base
    ),
    values_ready AS (
        SELECT *,
               CASE
                   WHEN rn <= {rsi_period} THEN NULL
                   WHEN avg_loss = 0 AND avg_gain > 0 THEN 100.0
                   WHEN avg_loss = 0 THEN 50.0
                   ELSE 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
               END AS rsi_value
        FROM indicators
    ),
    regimes AS (
        SELECT *,
               max(CASE WHEN rsi_value <= {rsi_buy} THEN date END) OVER stock_window AS last_rsi_entry,
               max(CASE WHEN rsi_value >= {rsi_sell} THEN date END) OVER stock_window AS last_rsi_exit,
               max(CASE WHEN close > turtle_entry_level THEN date END) OVER stock_window AS last_turtle_entry,
               max(CASE WHEN close < turtle_exit_level THEN date END) OVER stock_window AS last_turtle_exit
        FROM values_ready
        WINDOW stock_window AS (
            PARTITION BY instrument_id ORDER BY date
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        )
    ),
    eligible AS (
        SELECT *, ({score})::DOUBLE AS score
        FROM regimes
        WHERE date BETWEEN ? AND ?
          AND volume > 0 AND coalesce(amount, 0) > 0
          AND ({eligible})
    ),
    ranked AS (
        SELECT *, row_number() OVER (
            PARTITION BY date
            ORDER BY score DESC NULLS LAST, liquidity20 DESC NULLS LAST, instrument_id
        ) AS candidate_rank
        FROM eligible
        WHERE isfinite(score)
    )
    SELECT date AS signal_date, instrument_id, code, name, exchange,
           close AS signal_close, score, candidate_rank, liquidity20
    FROM ranked
    WHERE candidate_rank <= {int(top_n)}
    ORDER BY signal_date, candidate_rank
    """


def _load_market_frames(
    connection: duckdb.DuckDBPyConnection,
    scope_clause: str,
    warmup_start: pd.Timestamp,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    prefix = _complete_dates_sql(scope_clause)
    frame = connection.execute(
        f"""
        WITH {prefix},
        returns AS (
            SELECT r.date, r.instrument_id,
                   r.close / nullif(lag(r.close) OVER (
                       PARTITION BY r.instrument_id ORDER BY r.date
                   ), 0) - 1 AS daily_return
            FROM universe_raw r
            JOIN complete_dates USING (date)
        )
        SELECT c.date, c.stock_count, c.prior_20_median,
               avg(x.daily_return) FILTER (WHERE isfinite(x.daily_return)) AS benchmark_return
        FROM complete_dates c
        LEFT JOIN returns x USING (date)
        WHERE c.date BETWEEN ? AND ?
        GROUP BY c.date, c.stock_count, c.prior_20_median
        ORDER BY c.date
        """,
        [warmup_start.date(), end.date(), start.date(), end.date()],
    ).df()
    catalog = connection.execute(
        f"""SELECT count(*) AS universe_size,
                   count(*) FILTER (WHERE upper(coalesce(m.name, '')) LIKE '%ST%') AS current_st
            FROM instrument_master m
            WHERE m.asset_type='stock' AND {scope_clause}"""
    ).fetchone()
    total_dates = connection.execute(
        f"""
        WITH {_complete_dates_sql(scope_clause)}
        SELECT
            count(DISTINCT r.instrument_id) FILTER (WHERE r.date BETWEEN ? AND ?) AS universe_size,
            count(*) FILTER (
                WHERE r.date BETWEEN ? AND ? AND c.date IS NOT NULL
            ) AS daily_rows,
            count(*) FILTER (WHERE r.date BETWEEN ? AND ?) AS raw_daily_rows,
            count(DISTINCT r.date) FILTER (WHERE r.date BETWEEN ? AND ?) AS raw_dates,
            count(DISTINCT r.date) FILTER (
                WHERE r.date BETWEEN ? AND ? AND c.date IS NULL
            ) AS sparse_dates
        FROM universe_raw r
        LEFT JOIN complete_dates c USING (date)
        """,
        [
            warmup_start.date(), end.date(),
            start.date(), end.date(), start.date(), end.date(),
            start.date(), end.date(),
            start.date(), end.date(), start.date(), end.date(),
        ],
    ).fetchone()
    stats = {
        "universe_size": int(catalog[0] or 0),
        "eligible_universe_size": int(total_dates[0] or 0),
        "current_st_excluded": int(catalog[1] or 0),
        "daily_rows": int(total_dates[1] or 0),
        "raw_daily_rows": int(total_dates[2] or 0),
        "raw_dates": int(total_dates[3] or 0),
        "sparse_dates_excluded": int(total_dates[4] or 0),
    }
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"])
        frame["benchmark_return"] = frame["benchmark_return"].fillna(0.0)
    return frame, stats


def _apply_override_benchmark_and_stats(
    connection: duckdb.DuckDBPyConnection,
    market: pd.DataFrame,
    scope_clause: str,
    warmup_start: pd.Timestamp,
    start: pd.Timestamp,
    end: pd.Timestamp,
    calendar_stats: dict[str, Any],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Keep the broad-market calendar while measuring an injected universe.

    A small custom pool must not define the exchange calendar: one suspended
    member would otherwise delete the whole date and compress T+1.  Calendar
    completeness comes from the normal market scope, while benchmark and
    coverage below use only the validated override universe.
    """
    calendar = market[["date"]].copy()
    connection.register("override_complete_dates", calendar)
    returns = connection.execute(
        f"""
        WITH scoped AS (
            SELECT d.date, d.instrument_id, d.close,
                   d.close / nullif(lag(d.close) OVER (
                       PARTITION BY d.instrument_id ORDER BY d.date
                   ), 0) - 1 AS daily_return
            FROM daily_bar d
            JOIN instrument_master m USING (instrument_id)
            WHERE m.asset_type='stock' AND {scope_clause}
              AND upper(coalesce(m.name,'')) NOT LIKE '%ST%'
              AND d.date BETWEEN ? AND ? AND d.close>0
        )
        SELECT s.date,
               avg(s.daily_return) FILTER (WHERE isfinite(s.daily_return)) AS benchmark_return
        FROM scoped s JOIN override_complete_dates c USING (date)
        GROUP BY s.date ORDER BY s.date
        """,
        [warmup_start.date(), end.date()],
    ).df()
    benchmark = {
        pd.Timestamp(row.date): float(row.benchmark_return)
        for row in returns.itertuples(index=False)
        if pd.notna(row.benchmark_return)
    }
    market = market.copy()
    market["benchmark_return"] = market["date"].map(benchmark).fillna(0.0)
    catalog = connection.execute(
        f"""SELECT count(*),
                    count(*) FILTER (WHERE upper(coalesce(m.name,'')) LIKE '%ST%')
             FROM instrument_master m
             WHERE m.asset_type='stock' AND {scope_clause}"""
    ).fetchone()
    rows = connection.execute(
        f"""
        SELECT count(DISTINCT d.instrument_id), count(*), count(DISTINCT d.date)
        FROM daily_bar d
        JOIN instrument_master m USING (instrument_id)
        JOIN override_complete_dates c USING (date)
        WHERE m.asset_type='stock' AND {scope_clause}
          AND upper(coalesce(m.name,'')) NOT LIKE '%ST%'
          AND d.date BETWEEN ? AND ?
          AND d.open>0 AND d.high>0 AND d.low>0 AND d.close>0
        """,
        [start.date(), end.date()],
    ).fetchone()
    connection.unregister("override_complete_dates")
    stats = {
        "universe_size": int(catalog[0] or 0),
        "eligible_universe_size": int(rows[0] or 0),
        "current_st_excluded": int(catalog[1] or 0),
        "daily_rows": int(rows[1] or 0),
        "raw_daily_rows": int(rows[1] or 0),
        "raw_dates": int(rows[2] or 0),
        "sparse_dates_excluded": int(calendar_stats.get("sparse_dates_excluded", 0)),
        "calendar_source": "broad_market",
    }
    return market, stats


def _load_candidate_prices(
    connection: duckdb.DuckDBPyConnection,
    identifiers: list[str],
    warmup_start: pd.Timestamp,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    if not identifiers:
        return pd.DataFrame()
    frame = connection.execute(
        """
        WITH prices AS (
            SELECT d.date, d.instrument_id, m.code, m.name, m.exchange,
                   d.open, d.high, d.low, d.close, d.volume, d.amount,
                   lag(d.close) OVER (
                       PARTITION BY d.instrument_id ORDER BY d.date
                   ) AS prev_close,
                   avg(coalesce(d.amount, 0)) OVER (
                       PARTITION BY d.instrument_id ORDER BY d.date
                       ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
                   ) AS adv20_amount
            FROM daily_bar d
            JOIN instrument_master m USING (instrument_id)
            WHERE d.instrument_id IN (SELECT * FROM unnest(?))
              AND d.date BETWEEN ? AND ?
        )
        SELECT * FROM prices
        WHERE date BETWEEN ? AND ?
        ORDER BY date, instrument_id
        """,
        [identifiers, warmup_start.date(), end.date(), start.date(), end.date()],
    ).df()
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"])
    return frame


def _consume_lots(position: dict[str, Any], shares: int, day: pd.Timestamp) -> tuple[float, float, int, str]:
    """Consume T+1-available FIFO lots and return all-in/raw cost metadata."""
    remaining = shares
    all_in_cost = 0.0
    raw_cost = 0.0
    oldest_index: int | None = None
    entry_dates: list[str] = []
    new_lots: list[dict[str, Any]] = []
    for lot in position["lots"]:
        available = pd.Timestamp(lot["date"]) < day
        take = min(remaining, int(lot["shares"])) if available else 0
        if take:
            all_in_cost += take * float(lot["all_in_unit"])
            raw_cost += take * float(lot["price"])
            oldest_index = int(lot["day_index"]) if oldest_index is None else min(oldest_index, int(lot["day_index"]))
            entry_dates.append(str(pd.Timestamp(lot["date"]).date()))
            lot = {**lot, "shares": int(lot["shares"]) - take}
            remaining -= take
        if int(lot["shares"]) > 0:
            new_lots.append(lot)
    if remaining:
        raise RuntimeError("内部错误：卖出数量超过 T+1 可用数量")
    position["lots"] = new_lots
    position["shares"] = int(position["shares"]) - shares
    return all_in_cost, raw_cost, int(oldest_index or 0), min(entry_dates) if entry_dates else ""


def _active_gate_exit_schedule(
    signal_dates: Iterable[Any] | None,
    complete_days: list[pd.Timestamp],
) -> tuple[dict[pd.Timestamp, list[pd.Timestamp]], list[pd.Timestamp]]:
    """Map close-time gate exits to the strictly following complete day."""
    if signal_dates is None:
        return {}, []
    try:
        raw_dates = list(signal_dates)
    except TypeError as exc:
        raise ValueError("_active_gate_exit_signal_dates 必须是日期序列") from exc

    normalized: set[pd.Timestamp] = set()
    for value in raw_dates:
        try:
            stamp = pd.Timestamp(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("_active_gate_exit_signal_dates 包含无效日期") from exc
        if pd.isna(stamp):
            raise ValueError("_active_gate_exit_signal_dates 包含无效日期")
        if stamp.tz is not None:
            stamp = stamp.tz_localize(None)
        normalized.add(stamp.normalize())

    calendar = pd.DatetimeIndex(complete_days)
    schedule: dict[pd.Timestamp, list[pd.Timestamp]] = {}
    unmapped: list[pd.Timestamp] = []
    for signal_day in sorted(normalized):
        # ``side='right'`` guarantees T close -> T+1 open even when T itself is
        # in the complete calendar.  There is no final-bar fallback.
        execution_index = int(calendar.searchsorted(signal_day, side="right"))
        if execution_index >= len(calendar):
            unmapped.append(signal_day)
            continue
        execution_day = pd.Timestamp(calendar[execution_index])
        schedule.setdefault(execution_day, []).append(signal_day)
    return schedule, unmapped


def run_full_market_backtest(
    config: BacktestConfig,
    scope: str = "all",
    top_n: int = 20,
    rebalance: int = 5,
    persist: bool = True,
    *,
    _candidates: pd.DataFrame | None = None,
    _scope_clause_override: str | None = None,
    _active_gate_exit_signal_dates: Iterable[Any] | None = None,
    _max_position_pct: float = DEFAULT_MAX_POSITION_PCT,
) -> dict:
    """Run a vectorized full-market daily portfolio simulation.

    ``scope='all'`` includes Shanghai, Shenzhen and Beijing stocks;
    ``scope='sh_sz'`` excludes Beijing.  Only next-open execution is accepted,
    because every signal is frozen at the preceding close.
    """
    if config.strategy not in SUPPORTED_STRATEGIES:
        raise ValueError("全市场回测支持 buy_hold、ma_cross、rsi、momentum、turtle")
    if config.execution_mode != "next_open":
        raise ValueError("全市场日线回测固定为当日收盘信号、次日开盘成交")
    if scope not in VALID_SCOPES:
        raise ValueError("scope 只能是 all 或 sh_sz")
    top_n = int(top_n)
    rebalance = int(rebalance)
    if not 1 <= top_n <= 200:
        raise ValueError("top_n 必须在 1 到 200 之间")
    if not 1 <= rebalance <= 252:
        raise ValueError("rebalance 必须在 1 到 252 个交易日之间")
    max_position_pct = float(_max_position_pct)
    if not math.isfinite(max_position_pct) or not 0 < max_position_pct <= 1:
        raise ValueError("_max_position_pct 必须在 0 到 1 之间")
    invest_ratio = float(config.invest_ratio)
    if not math.isfinite(invest_ratio) or not 0 < invest_ratio <= 1:
        raise ValueError("invest_ratio 必须在 0 到 1 之间")
    # Allocation uses the planned position count, never the number of candidates
    # that happened to survive today.  Sparse candidate days therefore retain
    # cash instead of concentrating the whole exposure budget into one stock.
    target_position_pct = min(max_position_pct, invest_ratio / top_n)
    start = pd.Timestamp(config.start).normalize()
    end = pd.Timestamp(config.end).normalize()
    if end < start:
        raise ValueError("结束日期不能早于开始日期")
    if not MARKET_DB.exists():
        raise FileNotFoundError(f"本地行情库不存在: {MARKET_DB}")

    params = {
        **STRATEGIES[config.strategy].default_params,
        **(config.params or {}),
        "top_n": top_n,
        "rebalance": rebalance,
    }
    longest = max(
        int(params.get("slow", 20)),
        int(params.get("period", 14)),
        int(params.get("lookback", 20)),
        int(params.get("entry", 20)),
        20,
    )
    warmup_start = start - pd.Timedelta(days=max(120, longest * 4))
    # Private override is used by the validated Lego custom-universe block.
    # It is intentionally not part of the public free-form API: callers must
    # validate/sanitize custom codes before constructing this SQL predicate.
    calendar_scope_clause = _scope_clause(scope)
    scope_clause = _scope_clause_override or calendar_scope_clause

    with duckdb.connect(str(MARKET_DB), read_only=True) as connection:
        duckdb_threads = max(2, min(8, int(os.environ.get("QUANTWEAVE_DUCKDB_THREADS", "8"))))
        connection.execute(f"SET threads={duckdb_threads}")
        market, stats = _load_market_frames(
            connection, calendar_scope_clause, warmup_start, start, end
        )
        if _scope_clause_override is not None:
            market, stats = _apply_override_benchmark_and_stats(
                connection, market, scope_clause, warmup_start, start, end, stats
            )
        if market.empty or len(market) < 2:
            raise ValueError("所选区间没有至少两个完整市场交易日")
        candidates = (
            _candidates.copy()
            if _candidates is not None
            else connection.execute(
                _candidate_query(scope_clause, config.strategy, params, top_n),
                [warmup_start.date(), end.date(), start.date(), end.date()],
            ).df()
        )
        if candidates.empty:
            raise ValueError("所选区间没有符合策略条件的全市场候选股")
        candidates["signal_date"] = pd.to_datetime(candidates["signal_date"])
        identifiers = candidates["instrument_id"].astype(str).drop_duplicates().tolist()
        prices = _load_candidate_prices(connection, identifiers, warmup_start, start, end)

    if prices.empty:
        raise ValueError("候选股票缺少可撮合的本地日线")
    complete_days = list(market["date"])
    complete_set = set(complete_days)
    prices = prices[prices["date"].isin(complete_set)].copy()
    by_day = {
        day: part.set_index("instrument_id", drop=False)
        for day, part in prices.groupby("date", sort=True)
    }
    candidate_by_day = {
        day: part.sort_values("candidate_rank")
        for day, part in candidates.groupby("signal_date", sort=True)
    }
    active_gate_exit_schedule, active_gate_unmapped_signals = _active_gate_exit_schedule(
        _active_gate_exit_signal_dates, complete_days
    )

    # Close signal -> immediately following complete market day open execution.
    executions: dict[pd.Timestamp, list[str]] = {}
    execution_audit: dict[pd.Timestamp, dict[str, dict[str, Any]]] = {}
    audit_fields = [
        field for field in (
            "z_signal", "z_resonance_score", "z_line_state",
            "z_brick_color", "z_exit_reason",
        ) if field in candidates.columns
    ]

    def attach_execution_audit(execution_day: pd.Timestamp, part: pd.DataFrame | None) -> None:
        if part is None or part.empty or not audit_fields:
            return
        execution_audit[execution_day] = {
            str(row["instrument_id"]): {
                field: row[field]
                for field in audit_fields
                if pd.notna(row[field]) and row[field] != ""
            }
            for _, row in part.iterrows()
        }

    if config.strategy == "buy_hold":
        for index, signal_day in enumerate(complete_days[:-1]):
            part = candidate_by_day.get(signal_day)
            if part is not None and not part.empty:
                execution_day = complete_days[index + 1]
                executions[execution_day] = part["instrument_id"].astype(str).tolist()
                attach_execution_audit(execution_day, part)
                break
    else:
        for index, signal_day in enumerate(complete_days[:-1]):
            if index % rebalance:
                continue
            part = candidate_by_day.get(signal_day)
            targets = [] if part is None else part["instrument_id"].astype(str).tolist()
            execution_day = complete_days[index + 1]
            executions[execution_day] = targets
            attach_execution_audit(execution_day, part)

    rebalance_candidate_ids = [instrument_id for targets in executions.values() for instrument_id in targets]

    metadata = {
        str(row.instrument_id): {
            "code": str(row.code), "name": str(row.name), "exchange": str(row.exchange)
        }
        for row in candidates.drop_duplicates("instrument_id").itertuples(index=False)
    }
    cash = float(config.initial_cash)
    positions: dict[str, dict[str, Any]] = {}
    last_close: dict[str, float] = {}
    trade_rows: list[dict[str, Any]] = []
    closed_rows: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []
    benchmark_value = float(config.initial_cash)
    benchmark_returns = dict(zip(market["date"], market["benchmark_return"], strict=False))
    active_gate_exit_pending = False
    active_gate_attempt_dates: list[pd.Timestamp] = []
    active_gate_completion_dates: list[pd.Timestamp] = []
    active_gate_forced_trade_count = 0
    active_gate_forced_shares = 0

    for day_index, day in enumerate(complete_days):
        frame = by_day.get(day)
        if frame is not None:
            for row in frame.itertuples(index=False):
                if float(row.close or 0) > 0:
                    last_close[str(row.instrument_id)] = float(row.close)

        gate_exit_started_today = day in active_gate_exit_schedule
        if gate_exit_started_today:
            active_gate_exit_pending = True
        force_gate_exit = active_gate_exit_pending

        if day in executions or force_gate_exit:
            # Pending liquidation takes precedence over an ordinary rebalance.
            # The rebalance event is neither shifted nor replayed later.
            targets = [] if force_gate_exit else list(dict.fromkeys(executions[day]))[:top_n]
            target_set = set(targets)
            if force_gate_exit:
                active_gate_attempt_dates.append(day)
            open_nav = cash
            for instrument_id, position in positions.items():
                row = frame.loc[instrument_id] if frame is not None and instrument_id in frame.index else None
                mark = float(row.open) if row is not None and float(row.open or 0) > 0 else last_close.get(instrument_id, 0.0)
                open_nav += int(position["shares"]) * mark
            target_value = open_nav * target_position_pct if targets else 0.0

            # Sells first so proceeds can finance buys.  Lots bought today are
            # intentionally unavailable, implementing A-share T+1.
            for instrument_id in sorted(list(positions)):
                if frame is None or instrument_id not in frame.index:
                    continue  # no bar == suspension/no executable quote
                row = frame.loc[instrument_id]
                raw_open = float(row.open or 0)
                if raw_open <= 0:
                    continue
                info = metadata.get(instrument_id, {"code": str(row.code), "name": str(row["name"]), "exchange": str(row.exchange)})
                rate = _limit_rate(info["code"], info["exchange"], info.get("name", ""))
                prev_close = float(row.prev_close) if pd.notna(row.prev_close) else 0.0
                lower = round(prev_close * (1 - rate) + 1e-10, 2) if prev_close > 0 else 0.0
                if lower and raw_open <= lower + 0.001:
                    continue  # limit-down sell cannot fill
                desired = (
                    0
                    if force_gate_exit
                    else _floor_lot(target_value / raw_open, config.lot_size)
                    if instrument_id in target_set
                    else 0
                )
                available = sum(
                    int(lot["shares"])
                    for lot in positions[instrument_id]["lots"]
                    if pd.Timestamp(lot["date"]) < day
                )
                wanted = min(available, max(0, int(positions[instrument_id]["shares"]) - desired))
                # Use only turnover already known before this open.  Today's
                # full-day volume is future information and is stored in hands,
                # not shares, in the local PyTDX daily table.
                adv20_amount = float(row.adv20_amount) if pd.notna(row.adv20_amount) else 0.0
                capacity = _floor_lot(
                    adv20_amount * float(config.max_participation) / raw_open,
                    config.lot_size,
                )
                shares = _floor_lot(min(wanted, capacity), config.lot_size)
                if shares <= 0:
                    continue
                price = max(lower, raw_open * (1 - float(config.slippage))) if lower else raw_open * (1 - float(config.slippage))
                amount = shares * price
                fee = _fee(config, "卖出", amount)
                all_in_cost, raw_cost, opened_index, entry_date = _consume_lots(positions[instrument_id], shares, day)
                realized_pnl = amount - fee - all_in_cost
                realized_return = realized_pnl / all_in_cost if all_in_cost else None
                cash += amount - fee
                if positions[instrument_id]["shares"] <= 0:
                    positions.pop(instrument_id, None)
                trade_rows.append({
                    "date": day.date(), "code": instrument_id, "ticker": info["code"],
                    "name": info["name"], "symbol": f"{info['name']} {info['code']}",
                    "side": "卖出", "price": float(price), "shares": int(shares),
                    "amount": float(amount), "fee": float(fee),
                    "realized_pnl": float(realized_pnl), "realized_return": realized_return,
                    "reason": (
                        ACTIVE_GATE_EXIT_REASON
                        if gate_exit_started_today
                        else ACTIVE_GATE_RETRY_REASON
                        if force_gate_exit
                        else "全市场 TopN 调仓退出"
                    ),
                })
                if force_gate_exit:
                    active_gate_forced_trade_count += 1
                    active_gate_forced_shares += int(shares)
                closed_rows.append({
                    "code": instrument_id, "ticker": info["code"], "name": info["name"],
                    "entry_date": entry_date, "exit_date": day.date(), "shares": int(shares),
                    "pnl": float(realized_pnl), "gross_pnl": float(amount - raw_cost),
                    "realized_return": realized_return, "bars": int(day_index - opened_index),
                    "entry_cost": float(all_in_cost), "exit_proceeds": float(amount - fee),
                    "entry_price": float(raw_cost / shares) if shares else 0.0,
                    "exit_price": float(price),
                })

            if force_gate_exit and not positions:
                active_gate_exit_pending = False
                active_gate_completion_dates.append(day)

            # Forced-exit attempts always use an empty target list.  This also
            # blocks same-open re-entry after the final successful liquidation.
            open_exposure = 0.0
            for instrument_id, position in positions.items():
                position_row = frame.loc[instrument_id] if frame is not None and instrument_id in frame.index else None
                mark = (
                    float(position_row.open)
                    if position_row is not None and float(position_row.open or 0) > 0
                    else last_close.get(instrument_id, 0.0)
                )
                open_exposure += int(position["shares"]) * mark
            remaining_exposure_budget = max(0.0, open_nav * invest_ratio - open_exposure)
            for instrument_id in targets:
                if frame is None or instrument_id not in frame.index:
                    continue
                row = frame.loc[instrument_id]
                raw_open = float(row.open or 0)
                if raw_open <= 0:
                    continue
                info = metadata.get(instrument_id, {"code": str(row.code), "name": str(row["name"]), "exchange": str(row.exchange)})
                rate = _limit_rate(info["code"], info["exchange"], info.get("name", ""))
                prev_close = float(row.prev_close) if pd.notna(row.prev_close) else 0.0
                upper = round(prev_close * (1 + rate) + 1e-10, 2) if prev_close > 0 else math.inf
                if math.isfinite(upper) and raw_open >= upper - 0.001:
                    continue  # limit-up buy cannot fill
                price = min(upper, raw_open * (1 + float(config.slippage)))
                current = int(positions.get(instrument_id, {}).get("shares", 0))
                desired = _floor_lot(target_value / price, config.lot_size)
                wanted = max(0, desired - current)
                adv20_amount = float(row.adv20_amount) if pd.notna(row.adv20_amount) else 0.0
                capacity = _floor_lot(
                    adv20_amount * float(config.max_participation) / raw_open,
                    config.lot_size,
                )
                exposure_capacity = _floor_lot(
                    remaining_exposure_budget / price,
                    config.lot_size,
                )
                shares = _floor_lot(
                    min(wanted, capacity, exposure_capacity),
                    config.lot_size,
                )
                while shares > 0:
                    amount = shares * price
                    fee = _fee(config, "买入", amount)
                    if amount + fee <= cash + 1e-8:
                        break
                    shares -= int(config.lot_size)
                if shares <= 0:
                    continue
                amount = shares * price
                fee = _fee(config, "买入", amount)
                cash -= amount + fee
                remaining_exposure_budget = max(0.0, remaining_exposure_budget - amount)
                position = positions.setdefault(instrument_id, {"shares": 0, "lots": []})
                position["shares"] = int(position["shares"]) + shares
                position["lots"].append({
                    "date": day, "shares": shares, "price": float(price),
                    "all_in_unit": float((amount + fee) / shares), "day_index": day_index,
                })
                trade_rows.append({
                    "date": day.date(), "code": instrument_id, "ticker": info["code"],
                    "name": info["name"], "symbol": f"{info['name']} {info['code']}",
                    "side": "买入", "price": float(price), "shares": int(shares),
                    "amount": float(amount), "fee": float(fee),
                    "realized_pnl": None, "realized_return": None,
                    "reason": "前收盘信号、次日开盘 TopN 建仓",
                    **execution_audit.get(day, {}).get(instrument_id, {}),
                })

        equity = cash + sum(
            int(position["shares"]) * last_close.get(instrument_id, 0.0)
            for instrument_id, position in positions.items()
        )
        # Align both curves to identical initial equity on the first displayed
        # backtest day.  The first day's return is measured from a warm-up bar
        # and must not give the benchmark a head start before the chart begins.
        if day_index > 0:
            benchmark_value *= 1 + float(benchmark_returns.get(day, 0.0) or 0.0)
        equity_rows.append({
            "date": day, "equity": float(equity), "cash": float(cash),
            "benchmark": float(benchmark_value),
        })

    equity_frame = pd.DataFrame(equity_rows)
    equity_frame["drawdown"] = equity_frame["equity"] / equity_frame["equity"].cummax() - 1
    trade_columns = [
        "date", "code", "ticker", "name", "symbol", "side", "price", "shares",
        "amount", "fee", "realized_pnl", "realized_return", "reason",
        "z_signal", "z_resonance_score", "z_line_state", "z_brick_color",
        "z_exit_reason",
    ]
    closed_columns = [
        "code", "ticker", "name", "entry_date", "exit_date", "shares", "pnl",
        "gross_pnl", "realized_return", "bars", "entry_cost", "exit_proceeds",
        "entry_price", "exit_price",
    ]
    trades_frame = pd.DataFrame(trade_rows, columns=trade_columns)
    closed_frame = pd.DataFrame(closed_rows, columns=closed_columns)
    metrics = _metrics(equity_frame, trades_frame, closed_frame, config.initial_cash)
    result = {
        "run_id": datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "config": {
            **asdict(config), "start": str(config.start), "end": str(config.end),
            "params": params, "universe_scope": scope, "top_n": top_n,
            "rebalance": rebalance, "max_position_pct": max_position_pct,
            "target_position_pct": target_position_pct,
        },
        "strategy": STRATEGIES[config.strategy],
        "metrics": metrics,
        "coverage": {
            **stats, "universe_size": stats["universe_size"],
            "candidate_rows": int(len(rebalance_candidate_ids)), "dates": int(len(complete_days)),
            "instruments": int(len(set(rebalance_candidate_ids))),
            "signal_instruments": int(len(set(rebalance_candidate_ids))),
            "daily_rank_rows": int(len(candidates)),
            "daily_rank_instruments": int(candidates["instrument_id"].nunique()),
            "rebalance_events": int(len(executions)),
            "allocation": {
                "invest_ratio": invest_ratio,
                "max_position_pct": max_position_pct,
                "target_position_pct": target_position_pct,
                "planned_positions": top_n,
                "rule": "按计划持仓数分配且单票不超过上限；候选不足保留现金，总买入敞口不超过invest_ratio",
            },
            "active_gate_exit": {
                "enabled": _active_gate_exit_signal_dates is not None,
                "rule": "门控结束信号在收盘后确认，严格映射下一完整交易日开盘；未成交则逐个完整交易日重试，强退当日禁止回补，原调仓日历不重排",
                "trade_reason": ACTIVE_GATE_EXIT_REASON,
                "retry_trade_reason": ACTIVE_GATE_RETRY_REASON,
                "signal_count": int(
                    sum(len(days) for days in active_gate_exit_schedule.values())
                    + len(active_gate_unmapped_signals)
                ),
                "mapped_signal_count": int(
                    sum(len(days) for days in active_gate_exit_schedule.values())
                ),
                "unmapped_signal_dates": [
                    str(day.date()) for day in active_gate_unmapped_signals
                ],
                "signal_to_execution": [
                    {
                        "signal_date": str(signal_day.date()),
                        "execution_date": str(execution_day.date()),
                    }
                    for execution_day, signal_days in active_gate_exit_schedule.items()
                    for signal_day in signal_days
                ],
                "attempt_dates": [str(day.date()) for day in active_gate_attempt_dates],
                "attempts": [
                    {
                        "date": str(day.date()),
                        "reason": (
                            ACTIVE_GATE_EXIT_REASON
                            if day in active_gate_exit_schedule
                            else ACTIVE_GATE_RETRY_REASON
                        ),
                    }
                    for day in active_gate_attempt_dates
                ],
                "completion_dates": [str(day.date()) for day in active_gate_completion_dates],
                "same_open_reentry_blocked_dates": [
                    str(day.date()) for day in active_gate_attempt_dates
                ],
                "forced_trade_count": int(active_gate_forced_trade_count),
                "forced_shares": int(active_gate_forced_shares),
                "pending_at_end": bool(active_gate_exit_pending),
                "remaining_position_count": (
                    int(len(positions)) if active_gate_exit_pending else 0
                ),
                "remaining_shares": (
                    int(sum(int(position["shares"]) for position in positions.values()))
                    if active_gate_exit_pending
                    else 0
                ),
            },
            "sparse_date_rule": "当日股票数低于此前20个交易日中位数的80%时剔除",
            "capacity_rule": "仅使用执行日前已知的20日平均成交额限制订单容量",
            "price_adjustment": "unadjusted",
            "point_in_time_universe": False,
        },
        "equity": equity_frame,
        "trades": trades_frame,
        "closed_trades": closed_frame,
    }
    if persist:
        persist_result(result)
    return result


__all__ = ["run_full_market_backtest"]
