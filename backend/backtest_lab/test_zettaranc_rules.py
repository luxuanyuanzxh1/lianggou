from __future__ import annotations

import unittest

raise unittest.SkipTest("Z 系列已从回测工作站退役；历史规则仅保留归档")

from dataclasses import asdict
from unittest.mock import patch

import backtrader as bt
import duckdb
import pandas as pd

from quant_service.engine import GatePandasData
from quant_service.screeners.zettaranc_b2_confirmation import screen as screen_b2
from quant_service.screeners.zettaranc_b3_continuation import screen as screen_b3
from backtest_lab.lego_pipeline import (
    BLOCK_CATALOG,
    _raw_factor,
    _requirements,
    _attach_zettaranc_features,
    active_multifactor_pipeline,
    default_pipeline,
    validate_pipeline,
    zettaranc_pipeline,
)
from quant_service.strategies.zettaranc_b1_discipline import Strategy
from quant_service.zettaranc_rules import (
    _b2_mask,
    _core_b1_mask,
    _gate_ok,
    evaluate_b2,
    evaluate_b3,
    indicator_frame,
)


def _feature_rows(count: int = 6) -> pd.DataFrame:
    return pd.DataFrame({
        "kdj_j": [20.0] * count,
        "daily_pct": [0.0] * count,
        "amplitude": [3.0] * count,
        "volume_ratio": [0.7] * count,
        "n_lift": [True] * count,
        "green_brick": [False] * count,
        "green_brick_count": [0] * count,
        "s1": [False] * count,
        "close": [12.0] * count,
        "white_line": [11.0] * count,
        "yellow_line": [10.0] * count,
        "ma60": [10.5] * count,
        "macd_dif": [0.2] * count,
    })


class ZettarancRuleTests(unittest.TestCase):
    def test_kdj_recurrence_matches_independent_calculation(self) -> None:
        closes = [10 + index * 0.08 + (-0.25 if index % 5 == 0 else 0.0) for index in range(40)]
        bars = pd.DataFrame({
            "date": pd.date_range("2025-01-01", periods=len(closes), freq="D"),
            "open": [value - 0.05 for value in closes],
            "close": closes,
            "high": [value + 0.3 for value in closes],
            "low": [value - 0.4 for value in closes],
            "volume": [1_000_000 + index * 1000 for index in range(len(closes))],
            "amount": [10_000_000.0] * len(closes),
        })
        result = indicator_frame(bars)

        k_value = d_value = 50.0
        expected_j = []
        for index in range(len(bars)):
            if index < 8:
                k_value = d_value = 50.0
            else:
                window = bars.iloc[index - 8:index + 1]
                spread = float(window["high"].max() - window["low"].min())
                rsv = 50.0 if spread == 0 else (
                    float(bars.iloc[index]["close"] - window["low"].min()) / spread * 100.0
                )
                k_value = (2.0 * k_value + rsv) / 3.0
                d_value = (2.0 * d_value + k_value) / 3.0
            expected_j.append(3.0 * k_value - 2.0 * d_value)

        self.assertAlmostEqual(result.iloc[-1]["kdj_j"], expected_j[-1], places=12)

    def test_b1_j_threshold_is_inclusive(self) -> None:
        frame = _feature_rows(2)
        frame.loc[0, "kdj_j"] = -10.0
        frame.loc[1, "kdj_j"] = -9.99

        mask = _core_b1_mask(frame, {})

        self.assertTrue(bool(mask.iloc[0]))
        self.assertFalse(bool(mask.iloc[1]))

    def test_b2_accepts_t_minus_three_and_exact_four_percent(self) -> None:
        frame = _feature_rows(5)
        frame.loc[0, "kdj_j"] = -10.0
        frame.loc[3, ["daily_pct", "volume_ratio", "kdj_j"]] = [4.0, 1.2, 30.0]
        frame.loc[2, "kdj_j"] = 20.0

        mask = _b2_mask(frame, {})

        self.assertTrue(bool(mask.iloc[3]))
        frame.loc[3, "daily_pct"] = 3.99
        self.assertFalse(bool(_b2_mask(frame, {}).iloc[3]))

    def test_b2_rejects_b1_older_than_three_days(self) -> None:
        frame = _feature_rows(6)
        frame.loc[0, "kdj_j"] = -10.0
        frame.loc[4, ["daily_pct", "volume_ratio", "kdj_j"]] = [4.0, 1.2, 30.0]
        frame.loc[3, "kdj_j"] = 20.0

        self.assertFalse(bool(_b2_mask(frame, {}).iloc[4]))

    def test_constant_brick_series_stays_neutral(self) -> None:
        bars = pd.DataFrame({
            "date": pd.date_range("2025-01-01", periods=30, freq="D"),
            "open": [10.0] * 30,
            "close": [10.0] * 30,
            "high": [10.0] * 30,
            "low": [10.0] * 30,
            "volume": [1_000_000.0] * 30,
            "amount": [10_000_000.0] * 30,
        })

        result = indicator_frame(bars)

        self.assertFalse(bool(result["red_brick"].any()))
        self.assertFalse(bool(result["green_brick"].any()))
        self.assertEqual(int(result["red_brick_count"].max()), 0)
        self.assertEqual(int(result["green_brick_count"].max()), 0)

    def test_indicator_prefix_is_not_changed_by_future_bars(self) -> None:
        dates = pd.date_range("2024-01-01", periods=160, freq="D")
        close = pd.Series([10 + index * 0.02 + (index % 7) * 0.01 for index in range(160)])
        bars = pd.DataFrame({
            "date": dates,
            "open": close - 0.03,
            "close": close,
            "high": close + 0.15,
            "low": close - 0.16,
            "volume": [1_000_000 + (index % 11) * 10_000 for index in range(160)],
            "amount": [12_000_000.0] * 160,
        })

        short = indicator_frame(bars.iloc[:130])
        full = indicator_frame(bars)

        columns = ["kdj_j", "white_line", "yellow_line", "macd_dif", "brick"]
        pd.testing.assert_frame_equal(
            short[columns].reset_index(drop=True),
            full.iloc[:130][columns].reset_index(drop=True),
            check_exact=False,
            rtol=1e-12,
            atol=1e-12,
        )

    def test_closed_or_missing_gate_never_opens_required_strategy(self) -> None:
        self.assertEqual(_gate_ok({"use_active_gate": 1}), (False, {}))
        self.assertFalse(_gate_ok({"use_active_gate": 1, "_active_market_gate": {"enabled": False}})[0])
        self.assertTrue(_gate_ok({"use_active_gate": 0})[0])

    def test_backtrader_indicators_are_history_safe_on_flat_data(self) -> None:
        dates = pd.date_range("2024-01-01", periods=180, freq="D")
        frame = pd.DataFrame({
            "open": [10.0] * 180,
            "high": [10.0] * 180,
            "low": [10.0] * 180,
            "close": [10.0] * 180,
            "volume": [1_000_000.0] * 180,
            "openinterest": [0.0] * 180,
            "active_gate": [1.0] * 180,
            "industry_trend": [0.0] * 180,
            "concept_trend": [0.0] * 180,
        }, index=dates)
        cerebro = bt.Cerebro(stdstats=False)
        cerebro.adddata(GatePandasData(dataname=frame))
        cerebro.addstrategy(Strategy)
        cerebro.broker.setcash(1_000_000)

        result = cerebro.run(runonce=False)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].position.size, 0)
        self.assertLessEqual(float(result[0].p.position_pct), 0.10)

    def test_recursive_duckdb_features_match_python_reference(self) -> None:
        count = 130
        close = pd.Series([10 + index * 0.02 + (index % 7) * 0.01 for index in range(count)])
        source = pd.DataFrame({
            "date": pd.date_range("2025-01-01", periods=count, freq="D").date,
            "instrument_id": ["SH.600000"] * count,
            "code": ["600000"] * count,
            "name": ["测试"] * count,
            "exchange": ["SH"] * count,
            "open": close - 0.03,
            "high": close + 0.15,
            "low": close - 0.16,
            "close": close,
            "volume": [1_000_000 + (index % 11) * 10_000 for index in range(count)],
            "amount": [12_000_000.0] * count,
            "first_date": [pd.Timestamp("2025-01-01").date()] * count,
            "listing_days": list(range(count)),
            "adv_20": [12_000_000.0] * count,
        })
        source["prev_close"] = source["close"].shift(1)
        source["prev_volume"] = source["volume"].shift(1)
        source["daily_return"] = source["close"] / source["prev_close"] - 1.0
        for period in (14, 28, 57, 60, 114):
            source[f"ma_{period}"] = source["close"].rolling(period).mean()

        with duckdb.connect(":memory:") as connection:
            connection.register("source", source)
            connection.execute("CREATE TEMP TABLE lego_features AS SELECT * FROM source")
            _attach_zettaranc_features(connection)
            actual = connection.execute(
                "SELECT * FROM lego_features ORDER BY date"
            ).df()

        expected = indicator_frame(source[[
            "date", "open", "close", "high", "low", "volume", "amount"
        ]])
        pairs = {
            "z_kdj_j": "kdj_j",
            "z_white": "white_line",
            "z_yellow": "yellow_line",
            "z_macd_dif": "macd_dif",
            "z_brick": "brick",
        }
        for actual_name, expected_name in pairs.items():
            pd.testing.assert_series_equal(
                actual[actual_name].reset_index(drop=True),
                expected[expected_name].reset_index(drop=True),
                check_names=False,
                check_exact=False,
                rtol=1e-12,
                atol=1e-12,
            )
        self.assertTrue({
            "z_b1_standard", "z_b2_standard", "z_b3_standard",
            "z_exit_risk_standard", "z_signal", "z_resonance_score",
        }.issubset(actual.columns))

    def test_zettaranc_pipeline_requires_ths_gate_and_uses_eighty_percent_cap(self) -> None:
        preset = zettaranc_pipeline()
        validated = validate_pipeline(preset)
        types = {block.type for block in validated.blocks if block.enabled}
        execution = next(block for block in validated.blocks if block.type == "execution_next_open")

        self.assertIn("filter_ths_active_market_proxy_gate", types)
        self.assertIn("strategy_z_b123", types)
        self.assertIn("strategy_z_exit_risk_guard", types)
        self.assertIn("factor_z_resonance", types)
        self.assertIn("factor_zettaranc_quality", types)
        self.assertEqual(float(execution.params["invest_ratio"]), 0.80)
        self.assertEqual(float(execution.params["max_position_pct"]), 0.10)

        raw = {
            **preset.__dict__,
            "blocks": [
                block.__dict__ for block in preset.blocks
                if block.type != "filter_ths_active_market_proxy_gate"
            ],
        }
        with self.assertRaisesRegex(ValueError, "必须与同花顺活跃市值拟合门控串联"):
            validate_pipeline(raw)

    def test_brick_factor_is_runnable_and_loads_audited_z_features(self) -> None:
        self.assertTrue(BLOCK_CATALOG["factor_brick"]["available"])
        pipeline = asdict(default_pipeline())
        normalizer_index = next(
            index for index, block in enumerate(pipeline["blocks"])
            if block["type"] == "normalize_cross_section"
        )
        pipeline["blocks"].insert(normalizer_index, {
            "id": "factor-brick-test",
            "type": "factor_brick",
            "category": "factor",
            "name": "Z·砖形状态因子",
            "enabled": True,
            "combine": "AND",
            "params": {"weight": 0.1, "direction": "higher", "method": "percentile"},
        })

        validated = validate_pipeline(pipeline)

        self.assertTrue(_requirements(validated)["zettaranc"])
        brick = next(block for block in validated.blocks if block.type == "factor_brick")
        self.assertEqual(
            _raw_factor(brick),
            "coalesce(z_brick_state,0)+0.001*coalesce(z_brick,0)",
        )

    def test_independent_b2_and_b3_evaluators_only_return_their_stage(self) -> None:
        bars = pd.DataFrame()
        with patch("quant_service.zettaranc_rules.evaluate_b123") as evaluate:
            evaluate.return_value = {"score": 84.0, "details": {"阶段": "B2"}}
            self.assertIsNotNone(evaluate_b2(bars, {}))
            self.assertIsNone(evaluate_b3(bars, {}))

            evaluate.return_value = {"score": 92.0, "details": {"阶段": "B3"}}
            self.assertIsNone(evaluate_b2(bars, {}))
            self.assertIsNotNone(evaluate_b3(bars, {}))

        with patch("quant_service.screeners.zettaranc_b2_confirmation.evaluate_b2", return_value={"stage": "B2"}):
            self.assertEqual(screen_b2(bars, {}), {"stage": "B2"})
        with patch("quant_service.screeners.zettaranc_b3_continuation.evaluate_b3", return_value={"stage": "B3"}):
            self.assertEqual(screen_b3(bars, {}), {"stage": "B3"})

    def test_z_resonance_and_exit_guard_cannot_bypass_b123(self) -> None:
        preset = zettaranc_pipeline()
        raw = {
            **preset.__dict__,
            "blocks": [
                block.__dict__ for block in preset.blocks
                if block.type not in {
                    "strategy_z_b123", "strategy_z_exit_risk_guard",
                    "factor_zettaranc_quality",
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "阶段共振因子只能用于"):
            validate_pipeline(raw)

    def test_active_multifactor_preset_keeps_gate_hard_and_position_cap_at_ten_percent(self) -> None:
        validated = validate_pipeline(active_multifactor_pipeline())
        enabled = [block for block in validated.blocks if block.enabled]
        types = [block.type for block in enabled]

        self.assertIn("filter_ths_active_market_proxy_gate", types)
        self.assertNotIn("strategy_zettaranc_b1", types)
        self.assertLess(
            types.index("filter_ths_active_market_proxy_gate"),
            types.index("strategy_momentum"),
        )
        factors = {block.type: block for block in enabled if block.category == "factor"}
        self.assertEqual(
            set(factors),
            {"factor_momentum", "factor_trend", "factor_liquidity"},
        )
        self.assertEqual(factors["factor_momentum"].params["lookback"], 60)
        self.assertEqual(factors["factor_liquidity"].params["direction"], "lower")
        portfolio = next(block for block in enabled if block.type == "portfolio")
        execution = next(block for block in enabled if block.type == "execution_next_open")
        self.assertEqual(portfolio.params["positions"], 8)
        self.assertEqual(portfolio.params["rebalance_every"], 10)
        self.assertEqual(execution.params["invest_ratio"], 0.80)
        self.assertEqual(execution.params["max_position_pct"], 0.10)
        self.assertLessEqual(
            execution.params["invest_ratio"] / portfolio.params["positions"],
            0.10,
        )


if __name__ == "__main__":
    unittest.main()
