from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import backtrader as bt
import pandas as pd

from quant_service import engine
from quant_service.engine import AShareBackBroker, GatePandasData
from quant_service.strategy_loader import get_strategy
from quant_service.strategy_risk import (
    active_gate_is_open,
    capped_entry_size,
    latch_full_exit,
    partial_board_lot,
    retry_latched_full_exit,
    update_latched_full_exit,
)
from quant_service.strategies.b2_multifactor import Strategy as B2Strategy


STRATEGY_IDS = (
    "b1_multifactor",
    "b2_multifactor",
    "gated_multi_expert",
)


def _frame(*, periods: int, gate: list[float] | None) -> pd.DataFrame:
    dates = pd.bdate_range("2023-01-02", periods=periods)
    close = pd.Series([100.0 + offset * 0.1 for offset in range(periods)], index=dates)
    frame = pd.DataFrame({
        "open": close + 0.5,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": 1_000_000.0,
        "openinterest": 0.0,
    }, index=dates)
    if gate is not None:
        frame["active_gate"] = gate
        frame["industry_trend"] = 0.0
        frame["concept_trend"] = 0.0
    return frame


def _run(strategy, frame: pd.DataFrame, *, gate_feed: bool = True):
    cerebro = bt.Cerebro(stdstats=False)
    if gate_feed:
        data = GatePandasData(
            dataname=frame,
            active_gate="active_gate",
            industry_trend="industry_trend",
            concept_trend="concept_trend",
        )
    else:
        data = bt.feeds.PandasData(dataname=frame)
    cerebro.adddata(data)
    cerebro.addstrategy(strategy)
    cerebro.broker.setcash(1_000_000.0)
    cerebro.broker.set_coc(False)
    return cerebro.run(runonce=False)[0]


class StrategyRiskControlTests(unittest.TestCase):
    def test_five_strategies_default_to_proxy_hard_gate_and_exposure_caps(self) -> None:
        for strategy_id in STRATEGY_IDS:
            with self.subTest(strategy=strategy_id):
                _, meta = get_strategy(strategy_id)
                params = dict(meta["params"])
                self.assertEqual(params["active_provider"], "ths_internal_proxy")
                self.assertEqual(params["active_start_one"], 3.75)
                self.assertEqual(params["active_start_two"], 4.95)
                self.assertEqual(params["active_exit_one"], -2.05)
                self.assertTrue(params["active_use_two_day"])
                self.assertEqual(params["active_ma_buffer"], 0.0025)
                self.assertEqual(params["max_position_pct"], 0.10)
                self.assertEqual(params["max_total_exposure"], 0.80)

                config = engine._active_gate_config(params)
                self.assertEqual(config["provider"], "ths_internal_proxy")
                self.assertEqual(config["start_one"], 3.75)
                self.assertTrue(config["use_two_day"])
                self.assertEqual(config["ma_buffer"], 0.0025)

    def test_gate_missing_invalid_or_disabled_is_closed(self) -> None:
        class Line:
            def __init__(self, value):
                self.value = value

            def __getitem__(self, _):
                return self.value

        self.assertFalse(active_gate_is_open(SimpleNamespace()))
        self.assertFalse(active_gate_is_open(SimpleNamespace(active_gate=Line(float("nan")))))
        self.assertFalse(active_gate_is_open(SimpleNamespace(active_gate=Line(0.0))))
        self.assertTrue(active_gate_is_open(SimpleNamespace(active_gate=Line(1.0))))

    def test_stale_ths_proxy_file_fails_closed(self) -> None:
        payload = {
            "provider": "ths_internal_proxy",
            "quality": "close-only",
            "not_compass": True,
            "status": "stale",
            "stale": True,
            "candles": [
                {"date": "2026-07-17", "close": 100000.0},
                {"date": "2026-07-20", "close": 105000.0},
            ],
        }
        original = engine.THS_ACTIVE_PROXY_FILE
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proxy.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            engine.THS_ACTIVE_PROXY_FILE = path
            try:
                self.assertEqual(
                    engine.active_gate_series(provider="ths_internal_proxy"),
                    {},
                )
            finally:
                engine.THS_ACTIVE_PROXY_FILE = original

    def test_position_size_respects_total_room_as_well_as_single_name_cap(self) -> None:
        class Line:
            def __init__(self, value):
                self.value = value

            def __getitem__(self, _):
                return self.value

        target = SimpleNamespace(close=Line(10.0))
        existing = SimpleNamespace(close=Line(100.0))
        positions = {
            id(target): SimpleNamespace(size=0),
            id(existing): SimpleNamespace(size=7_500),
        }
        broker = SimpleNamespace(getvalue=lambda: 1_000_000.0, getcash=lambda: 1_000_000.0)
        strategy = SimpleNamespace(
            broker=broker,
            p=SimpleNamespace(max_position_pct=0.10, max_total_exposure=0.80, lot_size=100),
            data=target,
            datas=[target, existing],
            getposition=lambda feed: positions[id(feed)],
        )

        size = capped_entry_size(strategy, 10.0)
        self.assertEqual(size % 100, 0)
        self.assertLessEqual(size * 10.0, 50_000.0)
        self.assertLessEqual(size * 10.0, 100_000.0)

    def test_entry_caps_cannot_be_weakened_and_cover_a_legal_opening_gap(self) -> None:
        class Line:
            def __init__(self, value):
                self.value = value

            def __getitem__(self, _):
                return self.value

        target = SimpleNamespace(close=Line(10.0))
        broker = SimpleNamespace(getvalue=lambda: 1_000_000.0, getcash=lambda: 1_000_000.0)
        strategy = SimpleNamespace(
            broker=broker,
            p=SimpleNamespace(
                max_position_pct=1.0,
                max_total_exposure=1.0,
                lot_size=1,
                limit_pct=0.20,
            ),
            data=target,
            datas=[target],
            getposition=lambda _: SimpleNamespace(size=0),
        )

        size = capped_entry_size(strategy, 10.0)
        self.assertEqual(size % 100, 0)
        self.assertLessEqual(size * 12.0, 100_000.0)

    def test_partial_sale_is_always_a_board_lot(self) -> None:
        self.assertEqual(partial_board_lot(300), 100)
        self.assertEqual(partial_board_lot(200), 100)
        self.assertEqual(partial_board_lot(150), 0)

    def test_failed_full_exit_remains_latched_until_position_is_gone(self) -> None:
        class FakeOrder:
            Completed = 4
            Canceled = 5

            def __init__(self, status):
                self.status = status
                self.info = {}

            def addinfo(self, **kwargs):
                self.info.update(kwargs)

            def issell(self):
                return True

        class FakeStrategy:
            def __init__(self):
                self.position = SimpleNamespace(size=100)
                self.p = SimpleNamespace(limit_pct=0.10)
                self.orders = []

            def close(self):
                order = FakeOrder(FakeOrder.Canceled)
                self.orders.append(order)
                return order

        strategy = FakeStrategy()
        first = latch_full_exit(strategy, "门控关闭强退")
        update_latched_full_exit(strategy, first)
        second = retry_latched_full_exit(strategy)
        self.assertIsNotNone(second)
        self.assertEqual(len(strategy.orders), 2)
        self.assertEqual(strategy._latched_exit_reason, "门控关闭强退")

        strategy.position = None
        completed = FakeOrder(FakeOrder.Completed)
        update_latched_full_exit(strategy, completed)
        self.assertIsNone(strategy._latched_exit_reason)

    def test_a_share_broker_retries_limit_down_sell_on_next_open(self) -> None:
        class ExitAtNextClose(bt.Strategy):
            params = (("limit_pct", 0.10),)

            def __init__(self):
                self.pending = None
                self.sell_dates = []

            def next(self):
                if self.pending:
                    return
                if not self.position:
                    self.pending = self.buy(size=100)
                else:
                    self.pending = self.close()

            def notify_order(self, order):
                if order.status == order.Completed:
                    if order.issell():
                        self.sell_dates.append(bt.num2date(order.executed.dt).date())
                    self.pending = None

        dates = pd.bdate_range("2026-01-05", periods=4)
        frame = pd.DataFrame({
            "open": [10.0, 10.0, 9.0, 9.2],
            "high": [10.2, 10.2, 9.0, 9.4],
            "low": [9.8, 9.8, 9.0, 9.1],
            "close": [10.0, 10.0, 9.0, 9.3],
            "volume": [1_000_000.0] * 4,
            "openinterest": [0.0] * 4,
        }, index=dates)
        cerebro = bt.Cerebro(stdstats=False)
        cerebro.setbroker(AShareBackBroker())
        cerebro.adddata(bt.feeds.PandasData(dataname=frame))
        cerebro.addstrategy(ExitAtNextClose)
        cerebro.broker.setcash(1_000_000.0)
        result = cerebro.run(runonce=False)[0]

        self.assertEqual(result.sell_dates, [dates[3].date()])

    def test_limit_up_buy_is_cancelled_instead_of_filling_late(self) -> None:
        class BuyOnce(bt.Strategy):
            params = (("limit_pct", 0.10),)

            def __init__(self):
                self.issued = False
                self.completed_buys = 0
                self.cancelled_buys = 0

            def next(self):
                if not self.issued:
                    self.issued = True
                    self.buy(size=100)

            def notify_order(self, order):
                if not order.isbuy():
                    return
                if order.status == order.Completed:
                    self.completed_buys += 1
                elif order.status == order.Canceled:
                    self.cancelled_buys += 1

        dates = pd.bdate_range("2026-02-02", periods=3)
        frame = pd.DataFrame({
            "open": [10.0, 11.0, 10.5],
            "high": [10.2, 11.0, 10.8],
            "low": [9.8, 11.0, 10.4],
            "close": [10.0, 11.0, 10.6],
            "volume": [1_000_000.0] * 3,
            "openinterest": [0.0] * 3,
        }, index=dates)
        cerebro = bt.Cerebro(stdstats=False)
        cerebro.setbroker(AShareBackBroker())
        cerebro.adddata(bt.feeds.PandasData(dataname=frame))
        cerebro.addstrategy(BuyOnce)
        result = cerebro.run(runonce=False)[0]

        self.assertEqual(result.completed_buys, 0)
        self.assertEqual(result.cancelled_buys, 1)

    def test_b2_score_cannot_override_gate_or_hard_eligibility(self) -> None:
        class ForcedB2(B2Strategy):
            score_tuple = (100.0, 100.0, 100.0, 100.0, 100.0)

            def __init__(self):
                super().__init__()
                self.completed_buys = 0

            def _score(self):
                return self.score_tuple

            def _limit_up(self):
                return False

            def notify_order(self, order):
                if order.status == order.Completed and order.isbuy():
                    self.completed_buys += 1
                super().notify_order(order)

        gate_off = _run(ForcedB2, _frame(periods=90, gate=[0.0] * 90))
        self.assertEqual(gate_off.completed_buys, 0)

        class FailedHardSignal(ForcedB2):
            score_tuple = (100.0, 0.0, 100.0, 100.0, 100.0)

        failed_hard = _run(FailedHardSignal, _frame(periods=90, gate=[1.0] * 90))
        self.assertEqual(failed_hard.completed_buys, 0)


if __name__ == "__main__":
    unittest.main()
