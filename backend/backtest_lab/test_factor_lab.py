from __future__ import annotations

import unittest

from backtest_lab.factor_lab import (
    _build_pipeline,
    _selected_factors,
    factor_lab_catalog,
)
from backtest_lab.lego_pipeline import validate_pipeline


class FactorLabTests(unittest.TestCase):
    def test_catalog_exposes_factor_diagnostics_and_models(self) -> None:
        catalog = factor_lab_catalog()
        factor_ids = {item["id"] for item in catalog["factors"]}
        model_ids = {item["id"] for item in catalog["models"]}
        self.assertTrue({"momentum_20", "low_vol_20", "intraday_strength"}.issubset(factor_ids))
        self.assertTrue({"ridge", "hist_gb", "lightgbm", "xgboost"}.issubset(model_ids))
        self.assertIn("maturity_date", catalog["safety"]["label_policy"])

    def test_custom_formula_is_validated_and_weighted(self) -> None:
        selected = _selected_factors({
            "factors": [{"id": "momentum_20", "weight": 2.0}],
            "custom_factors": [{
                "name": "均线偏离", "formula": "close / MA(20) - 1",
                "direction": "higher", "weight": 1.0,
            }],
        })
        self.assertEqual(len(selected), 2)
        self.assertAlmostEqual(sum(item["weight"] for item in selected), 1.0)
        self.assertEqual(selected[1]["type"], "factor_custom_formula")

    def test_factor_pipeline_has_no_external_gate_and_keeps_position_caps(self) -> None:
        selected = _selected_factors({"factors": ["momentum_20", "low_vol_20"]})
        pipeline = validate_pipeline(_build_pipeline({
            "start_date": "2025-01-01", "end_date": "2026-07-20",
            "positions": 8, "rebalance_every": 10, "research_pool": 200,
        }, selected))
        types = {block.type for block in pipeline.blocks if block.enabled}
        self.assertNotIn("filter_ths_active_market_proxy_gate", types)
        self.assertNotIn("filter_active_market_gate", types)
        execution = next(block for block in pipeline.blocks if block.category == "execution")
        self.assertEqual(execution.params["invest_ratio"], 0.80)
        self.assertEqual(execution.params["lot_size"], 100)

if __name__ == "__main__":
    unittest.main()
