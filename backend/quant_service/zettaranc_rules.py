"""Deterministic, close-only Zettaranc rules adapted to Ruipan market data.

The upstream project is MIT licensed.  This module intentionally reimplements
only rules that can be reproduced from daily OHLCV data; it does not import the
upstream Tushare, SQLite, notification, LLM, or execution stack.
"""

from __future__ import annotations

from typing import Any

import pandas as pd


UPSTREAM = {
    "repository": "https://github.com/lululu811/zettaranc-skill",
    "version": "4.0.3",
    "license": "MIT",
    "skill_sha256": "9af12c7200222d26afae2aee5d9c114739f65fa8af69557838d479161bba2b03",
}


def _cn_sma(values: pd.Series, period: int, weight: int = 1) -> pd.Series:
    """TongdaXin ``SMA(X, N, M)`` recurrence without look-ahead.

    The first valid observation seeds the recurrence.  Missing warm-up values
    remain missing and do not silently count as red/green bricks.
    """
    output: list[float] = []
    previous: float | None = None
    for value in pd.to_numeric(values, errors="coerce"):
        if pd.isna(value):
            output.append(float("nan"))
            continue
        current = float(value)
        previous = current if previous is None else (
            weight * current + (period - weight) * previous
        ) / period
        output.append(previous)
    return pd.Series(output, index=values.index, dtype="float64")


def _consecutive_true(values: pd.Series) -> pd.Series:
    count = 0
    output: list[int] = []
    for value in values.fillna(False).astype(bool):
        count = count + 1 if value else 0
        output.append(count)
    return pd.Series(output, index=values.index, dtype="int64")


def _flag(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def indicator_frame(bars: pd.DataFrame) -> pd.DataFrame:
    """Return no-look-ahead KDJ, double-line, MACD, and candle features."""
    frame = bars.copy().sort_values("date").reset_index(drop=True)
    for column in ("open", "close", "high", "low", "volume", "amount"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    close = frame["close"]
    high = frame["high"]
    low = frame["low"]
    volume = frame["volume"].fillna(0.0)
    previous_close = close.shift(1)
    previous_volume = volume.shift(1)

    lowest = low.rolling(9, min_periods=9).min()
    highest = high.rolling(9, min_periods=9).max()
    spread = highest - lowest
    rsv = ((close - lowest) / spread * 100.0).where(spread.ne(0), 50.0)

    k_values: list[float] = []
    d_values: list[float] = []
    k_value = 50.0
    d_value = 50.0
    for index, value in enumerate(rsv):
        if index < 8 or pd.isna(value):
            k_value = 50.0
            d_value = 50.0
        else:
            k_value = (2.0 * k_value + float(value)) / 3.0
            d_value = (2.0 * d_value + k_value) / 3.0
        k_values.append(k_value)
        d_values.append(d_value)

    frame["kdj_k"] = pd.Series(k_values, dtype="float64")
    frame["kdj_d"] = pd.Series(d_values, dtype="float64")
    frame["kdj_j"] = 3.0 * frame["kdj_k"] - 2.0 * frame["kdj_d"]

    # TongdaXin-style EMA seeds from the first close.  Signals are still held
    # back until the longest 114-day moving average is available.
    ema10 = close.ewm(span=10, adjust=False).mean()
    frame["white_line"] = ema10.ewm(span=10, adjust=False).mean()
    frame["yellow_line"] = sum(
        close.rolling(period, min_periods=period).mean()
        for period in (14, 28, 57, 114)
    ) / 4.0
    frame["ma60"] = close.rolling(60, min_periods=60).mean()
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    frame["macd_dif"] = ema12 - ema26
    frame["macd_dea"] = frame["macd_dif"].ewm(span=9, adjust=False).mean()
    frame["daily_pct"] = (close / previous_close - 1.0) * 100.0
    frame["amplitude"] = (high - low) / previous_close * 100.0
    frame["volume_ratio"] = volume / previous_volume.where(previous_volume.ne(0))
    frame["shrinking_volume"] = volume.lt(previous_volume)
    frame["yin_count_4"] = close.lt(frame["open"]).rolling(4, min_periods=4).sum()

    # Four-brick formula from the audited upstream rule base.  Equal or all-zero
    # values are neutral; they must not create fictitious red-brick streaks.
    highest4 = high.rolling(4, min_periods=4).max()
    lowest4 = low.rolling(4, min_periods=4).min()
    range4 = highest4 - lowest4
    high_ratio = ((highest4 - close) / range4).where(range4.ne(0), 0.5)
    low_ratio = ((close - lowest4) / range4).where(range4.ne(0), 0.5)
    var1a = high_ratio * 100.0 - 90.0
    var2a = _cn_sma(var1a, 4, 1) + 100.0
    var3a = low_ratio * 100.0
    var4a = _cn_sma(var3a, 6, 1)
    var5a = _cn_sma(var4a, 6, 1) + 100.0
    frame["brick"] = (var5a - var2a - 4.0).clip(lower=0.0)
    brick_change = frame["brick"].diff()
    valid_brick = frame["brick"].notna() & frame["brick"].shift(1).notna()
    frame["red_brick"] = valid_brick & brick_change.gt(1e-12)
    frame["green_brick"] = valid_brick & brick_change.lt(-1e-12)
    frame["green_brick_count"] = _consecutive_true(frame["green_brick"])
    frame["red_brick_count"] = _consecutive_true(frame["red_brick"])

    # A fully deterministic N-shape proxy: the most recent ten-bar low may be
    # no more than 2% below the preceding ten-bar low.
    recent_low = low.rolling(10, min_periods=10).min()
    previous_low = low.shift(10).rolling(10, min_periods=10).min()
    frame["n_lift"] = recent_low.ge(previous_low * 0.98)
    frame["vol_ma20"] = volume.rolling(20, min_periods=20).mean()
    frame["s1"] = (
        close.lt(frame["open"])
        & frame["daily_pct"].le(-3.0)
        & frame["volume_ratio"].ge(1.5)
    ).fillna(False)
    return frame


def _core_b1_mask(frame: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    threshold = float(params.get("j_threshold", -10.0))
    green_limit = max(1, int(params.get("green_brick_limit", 4)))
    result = frame["kdj_j"].le(threshold)
    if _flag(params.get("require_b1_price_window"), True):
        result &= frame["daily_pct"].between(
            float(params.get("b1_min_gain", -2.0)),
            float(params.get("b1_max_gain", 1.8)),
            inclusive="both",
        )
    if _flag(params.get("require_b1_amplitude"), True):
        amplitude_limit = float(
            params.get("_b1_amplitude_limit", params.get("b1_max_amplitude", 7.0))
        )
        result &= frame["amplitude"].lt(amplitude_limit)
    if _flag(params.get("require_shrinking_volume"), True):
        result &= frame["volume_ratio"].le(float(params.get("b1_max_volume_ratio", 0.8)))
    if _flag(params.get("require_n_lift"), True):
        result &= frame["n_lift"]
    if _flag(params.get("forbid_any_green_brick"), True):
        result &= ~frame["green_brick"]
    else:
        result &= frame["green_brick_count"].lt(green_limit)
    if _flag(params.get("require_no_s1"), True):
        result &= ~frame["s1"]
    return result.fillna(False)


def _strict_context(frame: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    result = pd.Series(True, index=frame.index, dtype="bool")
    if _flag(params.get("require_above_white"), False):
        result &= frame["close"].gt(frame["white_line"])
    if _flag(params.get("require_above_yellow"), False):
        result &= frame["close"].gt(frame["yellow_line"])
    if _flag(params.get("require_white_above_yellow"), True):
        result &= frame["white_line"].ge(frame["yellow_line"])
    if _flag(params.get("require_support"), True):
        result &= (
            frame["close"].ge(frame["ma60"])
            | frame["close"].ge(frame["yellow_line"])
        )
    if _flag(params.get("require_macd_positive"), True):
        result &= frame["macd_dif"].ge(0.0)
    return result.fillna(False)


def _gate_ok(params: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    gate = params.get("_active_market_gate") or {}
    required = _flag(params.get("use_active_gate"), True)
    return (not required or bool(gate.get("enabled"))), gate


def _details(row: pd.Series, gate: dict[str, Any]) -> dict[str, Any]:
    return {
        "K": round(float(row["kdj_k"]), 2),
        "D": round(float(row["kdj_d"]), 2),
        "J": round(float(row["kdj_j"]), 2),
        "知行白线": round(float(row["white_line"]), 4),
        "多空黄线": round(float(row["yellow_line"]), 4),
        "MACD_DIF": round(float(row["macd_dif"]), 4),
        "砖形值": round(float(row["brick"]), 4),
        "砖形状态": "绿砖" if bool(row["green_brick"]) else "红砖" if bool(row["red_brick"]) else "中性",
        "连续绿砖": int(row["green_brick_count"]),
        "N型上移": bool(row["n_lift"]),
        "量比前日": round(float(row["volume_ratio"]), 3),
        "参考止损": round(float(row["low"]), 4),
        "活跃市值门控": gate,
        "规则来源": "zettaranc-skill v4.0.3 / MIT",
        "数据口径": "睿盘本地日线收盘，不含盘中9:33/9:37执行",
    }


def evaluate_b1(bars: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any] | None:
    frame = indicator_frame(bars)
    if len(frame) < 114:
        return None
    gate_ok, gate = _gate_ok(params)
    if not gate_ok:
        return None
    mask = _core_b1_mask(frame, params) & _strict_context(frame, params)
    if not bool(mask.iat[-1]):
        return None
    row = frame.iloc[-1]
    signals = [
        f"J={row['kdj_j']:.2f}≤{float(params.get('j_threshold', -10.0)):g}",
        f"涨跌幅{row['daily_pct']:.2f}%、振幅{row['amplitude']:.2f}%符合B1窗口",
        f"量比前日{row['volume_ratio']:.2f}，缩量回调",
        "当前不是绿砖，且N型低点未明显下移",
        "知行白线位于多空黄线上方，股价守住MA60或黄线",
        "MACD DIF位于零轴上方",
    ]
    if _flag(params.get("use_active_gate"), True):
        signals.insert(0, "同花顺活跃市值代理门控开启")
    score = 76.0 + (6.0 if row["volume_ratio"] <= 0.7 else 0.0)
    score += min(12.0, max(0.0, -float(row["kdj_j"]) - 10.0) * 0.5)
    return {"score": min(98.0, score), "signals": signals, "details": _details(row, gate)}


def _b2_mask(frame: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    b1 = _core_b1_mask(frame, params) & _strict_context(frame, params)
    min_age = max(1, int(params.get("b1_min_age", 1)))
    max_age = max(min_age, int(params.get("b1_max_age", 3)))
    has_recent_b1 = pd.Series(False, index=frame.index, dtype="bool")
    for age in range(min_age, max_age + 1):
        has_recent_b1 |= b1.shift(age, fill_value=False)
    minimum_gain = float(params.get("b2_min_gain", 4.0))
    maximum_j = float(params.get("b2_max_j", 55.0))
    return (
        has_recent_b1
        & frame["daily_pct"].ge(minimum_gain)
        & frame["volume_ratio"].ge(float(params.get("b2_min_volume_ratio", 1.2)))
        & frame["kdj_j"].lt(maximum_j)
        & frame["kdj_j"].gt(frame["kdj_j"].shift(1))
    ).fillna(False)


def _b3_mask(frame: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    b2 = _b2_mask(frame, params)
    lookback = max(1, int(params.get("b3_b2_lookback", 2)))
    has_recent_b2 = pd.Series(False, index=frame.index, dtype="bool")
    for age in range(1, lookback + 1):
        has_recent_b2 |= b2.shift(age, fill_value=False)
    return (
        has_recent_b2
        & frame["daily_pct"].gt(0.0)
        & frame["daily_pct"].lt(float(params.get("b3_max_gain", 2.0)))
        & frame["amplitude"].lt(float(params.get("b3_max_amplitude", 7.0)))
        & frame["volume_ratio"].le(float(params.get("b3_max_volume_ratio", 0.7)))
    ).fillna(False)


def evaluate_b123(bars: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any] | None:
    frame = indicator_frame(bars)
    if len(frame) < 114:
        return None
    gate_ok, gate = _gate_ok(params)
    if not gate_ok or not bool(_strict_context(frame, params).iat[-1]):
        return None
    row = frame.iloc[-1]
    b3 = _b3_mask(frame, params)
    b2 = _b2_mask(frame, params)
    b1 = _core_b1_mask(frame, params) & _strict_context(frame, params)
    if bool(b3.iat[-1]):
        stage, score = "B3", 92.0
        signals = [
            "B2后分歧转一致",
            f"涨幅{row['daily_pct']:.2f}%<2%",
            f"振幅{row['amplitude']:.2f}%<7%",
            f"量比前日{row['volume_ratio']:.2f}",
        ]
    elif bool(b2.iat[-1]):
        stage, score = "B2", 84.0
        signals = [
            "近期出现B1",
            f"放量长阳{row['daily_pct']:.2f}%",
            f"J值拐头至{row['kdj_j']:.2f}",
        ]
    elif bool(b1.iat[-1]):
        stage, score = "B1", 72.0
        signals = [
            f"J={row['kdj_j']:.2f}进入B1区",
            f"量比前日{row['volume_ratio']:.2f}",
            "当前非绿砖且N型低点上移",
        ]
    else:
        return None
    signals.insert(0, f"Z·三阶段：{stage}")
    if _flag(params.get("use_active_gate"), True):
        signals.insert(0, "同花顺活跃市值代理门控开启")
    details = _details(row, gate)
    details["阶段"] = stage
    return {"score": score, "signals": signals, "details": details}


def evaluate_b2(bars: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any] | None:
    """Return only a fully sequenced standard B2 confirmation."""
    result = evaluate_b123(bars, params)
    return result if result and result.get("details", {}).get("阶段") == "B2" else None


def evaluate_b3(bars: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any] | None:
    """Return only a fully sequenced standard B3 continuation."""
    result = evaluate_b123(bars, params)
    return result if result and result.get("details", {}).get("阶段") == "B3" else None
