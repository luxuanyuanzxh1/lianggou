from __future__ import annotations

import unittest

import duckdb
import numpy as np
import pandas as pd

from backtest_lab.lego_pipeline import (
    BLOCK_CATALOG,
    FACTOR_REGISTRY,
    BlockSpec,
    PipelineSpec,
    _attach_recursive_indicators,
    _feature_sql,
    _raw_factor,
    _requirements,
    standard_multifactor_pipeline,
    validate_pipeline,
)


REMOVED_TYPES = {
    "filter_active_market_gate",
    "filter_ths_active_market_proxy_gate",
    "strategy_active_market_gate",
    "strategy_zettaranc_b1",
    "strategy_z_b123",
    "strategy_z_b1_stage",
    "strategy_z_b2_stage",
    "strategy_z_b3_stage",
    "strategy_z_exit_risk_guard",
    "factor_zettaranc_quality",
    "factor_z_resonance",
    "factor_brick",
    "penalty_delta_rp",
}


class FactorRegistryTests(unittest.TestCase):
    def test_catalog_does_not_expose_personal_research_nodes(self) -> None:
        self.assertTrue(REMOVED_TYPES.isdisjoint(BLOCK_CATALOG))

    def test_catalog_contains_only_runnable_factors_and_no_retired_nodes(self) -> None:
        self.assertGreaterEqual(len(FACTOR_REGISTRY), 50)
        self.assertTrue(REMOVED_TYPES.isdisjoint(BLOCK_CATALOG))
        factors = {
            key: value for key, value in BLOCK_CATALOG.items()
            if value["category"] == "factor"
        }
        self.assertIn("factor_ma_cross_event", factors)
        self.assertNotIn("factor_brick", factors)
        self.assertTrue(all(item["available"] for item in factors.values()))
        self.assertTrue(all(item.get("point_in_time") for key, item in factors.items() if key in FACTOR_REGISTRY))
        teaching_fields = {"plain_explanation", "how_to_read", "best_for", "pitfall", "example_caption"}
        self.assertTrue(all(
            teaching_fields.issubset(item) and all(item[field] for field in teaching_fields)
            for key, item in factors.items() if key in FACTOR_REGISTRY
        ))

    def test_standard_preset_has_no_external_gate(self) -> None:
        pipeline = validate_pipeline(standard_multifactor_pipeline())
        types = {block.type for block in pipeline.blocks if block.enabled}
        self.assertTrue(REMOVED_TYPES.isdisjoint(types))
        self.assertEqual(pipeline.name, "标准稳健多因子")

    def test_every_registered_factor_compiles_and_returns_values(self) -> None:
        blocks = [
            BlockSpec(
                id=f"factor-{index}", type=factor_type, category="factor",
                name=definition["name"], params=dict(BLOCK_CATALOG[factor_type]["params"]),
            )
            for index, (factor_type, definition) in enumerate(FACTOR_REGISTRY.items())
            if factor_type in BLOCK_CATALOG
        ]
        spec = PipelineSpec(
            id="registry-smoke", name="registry-smoke", description="", version=1,
            scope="all", start="2025-01-01", end="2026-06-30",
            initial_cash=1_000_000.0, blocks=blocks,
        )
        requirements = _requirements(spec)
        dates = pd.bdate_range("2024-01-02", periods=340)
        masters: list[tuple[str, str, str, str, str]] = []
        bars: list[tuple[object, ...]] = []
        securities = [
            ("SH.600001", "600001", "SH"),
            ("SZ.000001", "000001", "SZ"),
            ("SZ.300001", "300001", "SZ"),
        ]
        for security_index, (instrument_id, code, exchange) in enumerate(securities):
            masters.append((instrument_id, code, f"测试{security_index}", exchange, "stock"))
            price = 10.0 + security_index * 3.0
            for row_index, timestamp in enumerate(dates):
                change = 0.0015 * np.sin(row_index / 7 + security_index) + 0.0003 * (security_index + 1)
                open_price = price * (1 + 0.001 * np.cos(row_index / 5))
                close = price * (1 + change)
                high, low = max(open_price, close) * 1.01, min(open_price, close) * 0.99
                volume = 1_000_000 * (1 + 0.25 * np.sin(row_index / 9 + security_index)) + 1_000 * row_index
                bars.append((timestamp.date(), instrument_id, open_price, high, low, close, volume, volume * close))
                price = close

        with duckdb.connect(":memory:") as connection:
            connection.execute(
                "CREATE TABLE instrument_master(instrument_id VARCHAR, code VARCHAR, "
                "name VARCHAR, exchange VARCHAR, asset_type VARCHAR)"
            )
            connection.executemany("INSERT INTO instrument_master VALUES (?,?,?,?,?)", masters)
            connection.execute(
                "CREATE TABLE daily_bar(date DATE, instrument_id VARCHAR, open DOUBLE, "
                "high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, amount DOUBLE)"
            )
            connection.executemany("INSERT INTO daily_bar VALUES (?,?,?,?,?,?,?,?)", bars)
            connection.execute(
                "CREATE TEMP TABLE lego_features AS "
                + _feature_sql("m.exchange IN ('SH','SZ','BJ')", requirements),
                [dates[0].date(), dates[-1].date()],
            )
            if requirements["recursive_indicators"]:
                _attach_recursive_indicators(connection, requirements)
            for block in blocks:
                with self.subTest(factor=block.type):
                    expression = _raw_factor(block)
                    self.assertTrue(expression)
                    count = connection.execute(
                        f"SELECT count(*) FILTER (WHERE ({expression}) IS NOT NULL) FROM lego_features"
                    ).fetchone()[0]
                    self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
