from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb
import pandas as pd

import backtest_lab.full_market as full_market
from backtest_lab.engine import BacktestConfig


class ActiveGateExecutionTests(unittest.TestCase):
    DAYS = pd.to_datetime(["2026-01-08", "2026-01-09", "2026-01-12", "2026-01-13"])

    def _candidates(self, signal_dates: list[str]) -> pd.DataFrame:
        return pd.DataFrame({
            "signal_date": pd.to_datetime(signal_dates),
            "instrument_id": ["SH.600000"] * len(signal_dates),
            "code": ["600000"] * len(signal_dates),
            "name": ["浦发银行"] * len(signal_dates),
            "exchange": ["SH"] * len(signal_dates),
            "signal_close": [10.0] * len(signal_dates),
            "score": [1.0] * len(signal_dates),
            "candidate_rank": [1] * len(signal_dates),
            "liquidity20": [1_000_000_000.0] * len(signal_dates),
            "z_signal": ["Z·B2"] * len(signal_dates),
            "z_resonance_score": [0.88] * len(signal_dates),
            "z_line_state": ["白线≥黄线"] * len(signal_dates),
            "z_brick_color": ["红砖"] * len(signal_dates),
            "z_exit_reason": [""] * len(signal_dates),
        })

    def _prices(self, monday_open: float = 10.0, monday_close: float = 10.0) -> pd.DataFrame:
        return pd.DataFrame({
            "date": self.DAYS,
            "instrument_id": ["SH.600000"] * len(self.DAYS),
            "code": ["600000"] * len(self.DAYS),
            "name": ["浦发银行"] * len(self.DAYS),
            "exchange": ["SH"] * len(self.DAYS),
            "open": [10.0, 10.0, monday_open, 9.2],
            "close": [10.0, 10.0, monday_close, 9.2],
            "prev_close": [10.0, 10.0, 10.0, monday_close],
            "adv20_amount": [1_000_000_000.0] * len(self.DAYS),
        })

    def _multi_candidate_market(self, count: int) -> tuple[pd.DataFrame, pd.DataFrame]:
        candidate_rows = []
        price_rows = []
        for index in range(count):
            code = f"{600000 + index:06d}"
            instrument_id = f"SH.{code}"
            candidate_rows.append({
                "signal_date": self.DAYS[0],
                "instrument_id": instrument_id,
                "code": code,
                "name": f"测试{index + 1}",
                "exchange": "SH",
                "signal_close": 10.0,
                "score": float(count - index),
                "candidate_rank": index + 1,
                "liquidity20": 1_000_000_000.0,
            })
            for day in self.DAYS:
                price_rows.append({
                    "date": day,
                    "instrument_id": instrument_id,
                    "code": code,
                    "name": f"测试{index + 1}",
                    "exchange": "SH",
                    "open": 10.0,
                    "close": 10.0,
                    "prev_close": 10.0,
                    "adv20_amount": 1_000_000_000.0,
                })
        return pd.DataFrame(candidate_rows), pd.DataFrame(price_rows)

    def _run(
        self,
        candidates: pd.DataFrame,
        prices: pd.DataFrame,
        *,
        rebalance: int,
        top_n: int = 1,
        invest_ratio: float = 0.95,
        active_gate_exit_signal_dates: tuple[str, ...] | list[str] | None = ("2026-01-09",),
    ) -> dict:
        market = pd.DataFrame({
            "date": self.DAYS,
            "benchmark_return": [0.0] * len(self.DAYS),
        })
        stats = {"universe_size": 1}
        config = BacktestConfig(
            strategy="momentum",
            codes=[],
            start=self.DAYS[0].date(),
            end=self.DAYS[-1].date(),
            universe_scope="all",
            initial_cash=100_000.0,
            execution_mode="next_open",
            commission=0.0,
            min_commission=0.0,
            transfer_fee=0.0,
            stamp_tax=0.0,
            slippage=0.0,
            invest_ratio=invest_ratio,
            lot_size=100,
            max_participation=1.0,
        )
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "market.duckdb"
            duckdb.connect(str(database)).close()
            with (
                patch.object(full_market, "MARKET_DB", database),
                patch.object(
                    full_market,
                    "_load_market_frames",
                    return_value=(market.copy(), stats.copy()),
                ),
                patch.object(
                    full_market,
                    "_load_candidate_prices",
                    return_value=prices.copy(),
                ),
            ):
                return full_market.run_full_market_backtest(
                    config,
                    top_n=top_n,
                    rebalance=rebalance,
                    persist=False,
                    _candidates=candidates,
                    _active_gate_exit_signal_dates=active_gate_exit_signal_dates,
                )

    def test_schedule_is_strictly_next_complete_day_and_audits_final_signal(self) -> None:
        schedule, unmapped = full_market._active_gate_exit_schedule(
            ["2026-01-09", "2026-01-13"], list(self.DAYS)
        )

        self.assertEqual(list(schedule), [pd.Timestamp("2026-01-12")])
        self.assertEqual(schedule[pd.Timestamp("2026-01-12")], [pd.Timestamp("2026-01-09")])
        self.assertEqual(unmapped, [pd.Timestamp("2026-01-13")])

    def test_gate_exit_wins_over_rebalance_and_blocks_same_open_reentry(self) -> None:
        result = self._run(
            self._candidates(["2026-01-08", "2026-01-09"]),
            self._prices(),
            rebalance=1,
        )

        trades = result["trades"]
        self.assertEqual(trades["side"].tolist(), ["买入", "卖出"])
        self.assertEqual(trades["date"].tolist(), [self.DAYS[1].date(), self.DAYS[2].date()])
        self.assertEqual(trades.iloc[0]["z_signal"], "Z·B2")
        self.assertAlmostEqual(float(trades.iloc[0]["z_resonance_score"]), 0.88)
        self.assertEqual(trades.iloc[-1]["reason"], full_market.ACTIVE_GATE_EXIT_REASON)
        self.assertFalse(
            ((trades["date"] == self.DAYS[2].date()) & (trades["side"] == "买入")).any()
        )
        audit = result["coverage"]["active_gate_exit"]
        self.assertEqual(audit["signal_to_execution"], [{
            "signal_date": "2026-01-09",
            "execution_date": "2026-01-12",
        }])
        self.assertEqual(audit["attempt_dates"], ["2026-01-12"])
        self.assertEqual(audit["completion_dates"], ["2026-01-12"])
        self.assertEqual(audit["same_open_reentry_blocked_dates"], ["2026-01-12"])
        self.assertFalse(audit["pending_at_end"])

    def test_limit_down_exit_retries_on_each_complete_day(self) -> None:
        result = self._run(
            self._candidates(["2026-01-08"]),
            self._prices(monday_open=9.0, monday_close=9.0),
            rebalance=5,
        )

        sells = result["trades"].loc[result["trades"]["side"] == "卖出"]
        self.assertEqual(sells["date"].tolist(), [self.DAYS[3].date()])
        self.assertEqual(sells.iloc[0]["reason"], full_market.ACTIVE_GATE_RETRY_REASON)
        audit = result["coverage"]["active_gate_exit"]
        self.assertEqual(audit["attempt_dates"], ["2026-01-12", "2026-01-13"])
        self.assertEqual(
            [attempt["reason"] for attempt in audit["attempts"]],
            [full_market.ACTIVE_GATE_EXIT_REASON, full_market.ACTIVE_GATE_RETRY_REASON],
        )
        self.assertEqual(audit["completion_dates"], ["2026-01-13"])
        self.assertEqual(audit["forced_trade_count"], 1)
        self.assertGreater(audit["forced_shares"], 0)
        self.assertFalse(audit["pending_at_end"])

    def test_one_candidate_never_absorbs_eighty_percent_exposure(self) -> None:
        result = self._run(
            self._candidates(["2026-01-08"]),
            self._prices(),
            rebalance=5,
            top_n=8,
            invest_ratio=0.80,
            active_gate_exit_signal_dates=None,
        )

        buys = result["trades"].loc[result["trades"]["side"] == "买入"]
        self.assertEqual(len(buys), 1)
        self.assertLessEqual(float(buys["amount"].sum()), 100_000.0 * 0.10 + 1e-8)
        self.assertGreaterEqual(float(result["equity"].iloc[1]["cash"]), 90_000.0 - 1e-8)
        self.assertAlmostEqual(
            result["coverage"]["allocation"]["target_position_pct"], 0.10,
        )

    def test_eight_candidates_respect_single_and_total_exposure_caps(self) -> None:
        candidates, prices = self._multi_candidate_market(8)
        result = self._run(
            candidates,
            prices,
            rebalance=5,
            top_n=8,
            invest_ratio=0.80,
            active_gate_exit_signal_dates=None,
        )

        buys = result["trades"].loc[result["trades"]["side"] == "买入"]
        self.assertEqual(len(buys), 8)
        self.assertLessEqual(float(buys["amount"].max()), 100_000.0 * 0.10 + 1e-8)
        self.assertLessEqual(float(buys["amount"].sum()), 100_000.0 * 0.80 + 1e-8)
        self.assertGreaterEqual(float(result["equity"].iloc[1]["cash"]), 20_000.0 - 1e-8)


if __name__ == "__main__":
    unittest.main()
