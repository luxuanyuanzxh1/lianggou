from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import duckdb
import pandas as pd

from backtest_lab.market_updater import (
    UpdatePlan,
    normalize_daily_rows,
    run_market_update,
    write_legacy_eod_database,
    write_market_database,
)


def make_market_database(path: Path) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE daily_bar(date DATE, instrument_id VARCHAR, open DOUBLE, close DOUBLE, high DOUBLE, low DOUBLE, volume DOUBLE, amount DOUBLE)")
        connection.execute("CREATE TABLE sync_state(instrument_id VARCHAR PRIMARY KEY, status VARCHAR, rows BIGINT, min_date DATE, max_date DATE, updated_at TIMESTAMP, error VARCHAR)")
        for table in ("weekly_bar", "monthly_bar", "quarterly_bar", "yearly_bar"):
            connection.execute(f"CREATE TABLE {table}(period_start DATE, instrument_id VARCHAR, open DOUBLE, close DOUBLE, high DOUBLE, low DOUBLE, volume DOUBLE, amount DOUBLE, trading_days BIGINT, period_end DATE)")


def make_legacy_database(path: Path) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE daily_bar(date DATE, code VARCHAR, open DOUBLE, close DOUBLE, high DOUBLE, low DOUBLE, volume DOUBLE, amount DOUBLE)")
        connection.execute("CREATE TABLE update_log(run_at TIMESTAMP, mode VARCHAR, source VARCHAR, rows_written BIGINT, message VARCHAR)")
        for table in ("weekly_bar", "monthly_bar", "quarterly_bar", "yearly_bar"):
            connection.execute(f"CREATE TABLE {table}(period_start DATE, code VARCHAR, open DOUBLE, close DOUBLE, high DOUBLE, low DOUBLE, volume DOUBLE, amount DOUBLE, trading_days BIGINT, period_end DATE)")


class MarketUpdaterTests(unittest.TestCase):
    @patch("backtest_lab.market_updater.update_features")
    @patch("backtest_lab.market_updater.market_snapshot", return_value={"dataset_version": 2})
    @patch("backtest_lab.market_updater.write_market_database", return_value=0)
    @patch("backtest_lab.market_updater.fetch_incremental_daily_bars")
    @patch("backtest_lab.market_updater.load_update_plan")
    def test_market_only_update_skips_precomputation(
        self,
        load_plan,
        fetch_bars,
        _write_market,
        _snapshot,
        update_features_mock,
    ) -> None:
        load_plan.return_value = (
            UpdatePlan(date(2026, 8, 18), date(2026, 8, 17), 1, 0, 0),
            [],
        )
        fetch_bars.return_value = (pd.DataFrame(), [], "not-needed")

        result = run_market_update(update_precomputed=False, write_legacy=False)

        update_features_mock.assert_not_called()
        self.assertFalse(result["precompute_enabled"])
        self.assertIsNone(result["precompute_ok"])

    def test_normalize_filters_old_and_invalid_rows(self) -> None:
        instrument = {"instrument_id": "SH.600000", "code": "600000", "asset_type": "stock"}
        frame = normalize_daily_rows(
            [
                {"datetime": "2026-08-03", "open": 1, "close": 1, "high": 1, "low": 1, "vol": 10, "amount": 10},
                {"datetime": "2026-08-04", "open": 2, "close": 2, "high": 2, "low": 2, "vol": 20, "amount": 20},
            ],
            instrument,
            date(2026, 8, 4),
            date(2026, 8, 4),
        )
        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.iloc[0]["date"], date(2026, 8, 4))
        self.assertEqual(frame.iloc[0]["volume"], 20)

    def test_write_updates_market_periods_and_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            market = root / "market.duckdb"
            legacy = root / "legacy.duckdb"
            make_market_database(market)
            make_legacy_database(legacy)
            frame = pd.DataFrame([
                {"date": date(2026, 8, 3), "instrument_id": "SH.600000", "code": "600000", "asset_type": "stock", "open": 10.0, "close": 11.0, "high": 11.5, "low": 9.5, "volume": 100.0, "amount": 1000.0},
                {"date": date(2026, 8, 4), "instrument_id": "SH.600000", "code": "600000", "asset_type": "stock", "open": 11.0, "close": 12.0, "high": 12.5, "low": 10.5, "volume": 200.0, "amount": 2200.0},
            ])
            self.assertEqual(write_market_database(frame, market), 2)
            self.assertEqual(write_legacy_eod_database(frame, "test", legacy), 2)
            with duckdb.connect(str(market), read_only=True) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM daily_bar").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT close FROM weekly_bar").fetchone()[0], 12.0)
                self.assertEqual(connection.execute("SELECT rows FROM sync_state").fetchone()[0], 2)
            with duckdb.connect(str(legacy), read_only=True) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM daily_bar").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT rows_written FROM update_log").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT close FROM weekly_bar").fetchone()[0], 12.0)


if __name__ == "__main__":
    unittest.main()
