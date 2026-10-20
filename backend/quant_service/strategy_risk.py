"""Shared hard risk controls for single-security Backtrader strategies."""

from __future__ import annotations

import math
from typing import Any


THS_PROXY_GATE_PARAMS = {
    "active_provider": "ths_internal_proxy",
    "active_start_one": 3.75,
    "active_start_two": 4.95,
    "active_exit_one": -2.05,
    "active_ma": 10,
    "active_use_two_day": True,
    "active_ma_buffer": 0.0025,
}

DEFAULT_EXPOSURE_PARAMS = {
    "max_position_pct": 0.10,
    "max_total_exposure": 0.80,
    "lot_size": 100,
}

HARD_MAX_POSITION_PCT = 0.10
HARD_MAX_TOTAL_EXPOSURE = 0.80
HARD_BOARD_LOT = 100


def active_gate_is_open(data: Any) -> bool:
    """Fail closed when the gate line is absent, invalid, or not enabled."""
    line = getattr(data, "active_gate", None)
    if line is None:
        return False
    try:
        value = float(line[0])
    except (IndexError, TypeError, ValueError):
        return False
    return math.isfinite(value) and value > 0.5


def _bounded_ratio(value: Any, hard_cap: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return hard_cap
    if not math.isfinite(number):
        return hard_cap
    return max(0.0, min(hard_cap, number))


def _board_lot(value: Any) -> int:
    """Return an A-share buy lot that cannot be weakened below 100 shares."""
    try:
        requested = int(value)
    except (TypeError, ValueError):
        requested = HARD_BOARD_LOT
    requested = max(HARD_BOARD_LOT, requested)
    return int(math.ceil(requested / HARD_BOARD_LOT) * HARD_BOARD_LOT)


def capped_entry_size(strategy: Any, price: float, *, data: Any | None = None) -> int:
    """Return a board-lot entry size respecting per-name and gross caps."""
    try:
        mark = float(price)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(mark) or mark <= 0:
        return 0

    portfolio_value = max(0.0, float(strategy.broker.getvalue()))
    cash = max(0.0, float(strategy.broker.getcash()))
    if portfolio_value <= 0 or cash <= 0:
        return 0

    per_name = _bounded_ratio(
        getattr(strategy.p, "max_position_pct", HARD_MAX_POSITION_PCT),
        HARD_MAX_POSITION_PCT,
    )
    gross_cap = _bounded_ratio(
        getattr(strategy.p, "max_total_exposure", HARD_MAX_TOTAL_EXPOSURE),
        HARD_MAX_TOTAL_EXPOSURE,
    )
    lot_size = _board_lot(getattr(strategy.p, "lot_size", HARD_BOARD_LOT))
    target_data = data if data is not None else strategy.data

    gross_exposure = 0.0
    target_exposure = 0.0
    for feed in getattr(strategy, "datas", [target_data]):
        position = strategy.getposition(feed)
        try:
            feed_mark = float(feed.close[0])
        except (IndexError, TypeError, ValueError):
            feed_mark = 0.0
        if not math.isfinite(feed_mark) or feed_mark <= 0:
            feed_mark = mark if feed is target_data else 0.0
        exposure = abs(float(getattr(position, "size", 0) or 0) * feed_mark)
        gross_exposure += exposure
        if feed is target_data:
            target_exposure = exposure

    position_room = max(0.0, portfolio_value * per_name - target_exposure)
    gross_room = max(0.0, portfolio_value * gross_cap - gross_exposure)
    # Signals are generated at T close and executed at T+1 open.  Size against
    # the next day's theoretical A-share limit-up price so a legal opening gap
    # cannot push a nominal 10% order through the hard cap.
    try:
        limit_pct = float(getattr(strategy.p, "limit_pct", 0.10))
    except (TypeError, ValueError):
        limit_pct = 0.10
    if not math.isfinite(limit_pct):
        limit_pct = 0.10
    limit_pct = max(0.0, min(0.30, limit_pct))
    sizing_price = mark * (1.0 + limit_pct)

    # Leave a small fee/slippage reserve as well.
    budget = min(cash, position_room, gross_room) * 0.999
    return max(0, int(budget / sizing_price / lot_size) * lot_size)


def latch_full_exit(strategy: Any, reason: str):
    """Submit a full exit and retain its reason until liquidation completes."""
    strategy._latched_exit_reason = str(reason)
    order = strategy.close()
    if order is not None:
        order.addinfo(
            reason=str(reason),
            latched_full_exit=True,
            limit_pct=float(getattr(strategy.p, "limit_pct", 0.10)),
        )
    return order


def retry_latched_full_exit(strategy: Any):
    """Retry a previously failed/cancelled exit without consulting new entries."""
    reason = getattr(strategy, "_latched_exit_reason", None)
    if not getattr(strategy, "position", None):
        strategy._latched_exit_reason = None
        return None
    if not reason:
        return None
    return latch_full_exit(strategy, str(reason))


def update_latched_full_exit(strategy: Any, order: Any) -> None:
    """Clear the latch only after the position has actually been liquidated."""
    if (
        order.status == order.Completed
        and order.issell()
        and not getattr(strategy, "position", None)
    ):
        strategy._latched_exit_reason = None


def partial_board_lot(position_size: Any, *, fraction: float = 0.5) -> int:
    """Floor a partial A-share sale to a valid 100-share board lot."""
    try:
        shares = max(0, abs(int(position_size)))
        ratio = float(fraction)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(ratio) or ratio <= 0:
        return 0
    return int((shares * min(1.0, ratio)) // HARD_BOARD_LOT) * HARD_BOARD_LOT
