from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

from backtest_lab.precompute import (
    build_features,
    precompute_status,
    record_market_change,
    update_features,
    verify_features,
)


class PrecomputeTests(unittest.TestCase):
    def _market(self, root: Path) -> Path:
        path = root / "market.duckdb"
        dates = pd.bdate_range("2023-01-02", periods=180).date
        rows = pd.DataFrame({
            "date": list(dates),
            "instrument_id": ["SH.600000"] * len(dates),
            "open": [10.0 + index / 100 for index in range(len(dates))],
            "close": [10.05 + index / 100 for index in range(len(dates))],
            "high": [10.10 + index / 100 for index in range(len(dates))],
            "low": [9.95 + index / 100 for index in range(len(dates))],
            "volume": [1_000_000.0] * len(dates),
            "amount": [50_000_000.0] * len(dates),
        })
        with duckdb.connect(str(path)) as connection:
            connection.execute(
                """CREATE TABLE instrument_master(
                       instrument_id VARCHAR PRIMARY KEY,market INTEGER,code VARCHAR,name VARCHAR,
                       asset_type VARCHAR,exchange VARCHAR,source VARCHAR,updated_at TIMESTAMP)"""
            )
            connection.execute(
                "INSERT INTO instrument_master VALUES ('SH.600000',1,'600000','浦发银行','stock','SH','test',current_timestamp)"
            )
            connection.execute(
                """CREATE TABLE daily_bar(
                       date DATE,instrument_id VARCHAR,open DOUBLE,close DOUBLE,high DOUBLE,low DOUBLE,
                       volume DOUBLE,amount DOUBLE,PRIMARY KEY(date,instrument_id))"""
            )
            connection.register("fixture_rows", rows)
            connection.execute("INSERT INTO daily_bar SELECT * FROM fixture_rows")
        return path

    def test_build_verify_and_incremental_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            market = self._market(root)
            features = root / "features.duckdb"
            built = build_features(market, features, history_years=10)
            self.assertTrue(built["ok"])
            self.assertEqual(built["row_count"], 180)
            self.assertTrue(verify_features(market, features)["ok"])
            with duckdb.connect(str(features), read_only=True) as connection:
                prior_feature = connection.execute(
                    "SELECT ma_20 FROM daily_core_features ORDER BY date DESC LIMIT 1"
                ).fetchone()[0]

            new_date = date(2023, 9, 11)
            change = pd.DataFrame({"instrument_id": ["SH.600000"], "date": [new_date]})
            with duckdb.connect(str(market)) as connection:
                connection.execute("BEGIN TRANSACTION")
                connection.execute(
                    "INSERT INTO daily_bar VALUES (?,?,?,?,?,?,?,?)",
                    [new_date, "SH.600000", 12.0, 12.1, 12.2, 11.9, 1_000_000.0, 50_000_000.0],
                )
                record_market_change(connection, change)
                connection.execute("COMMIT")
            self.assertEqual(precompute_status(market, features)["status"], "stale")
            updated = update_features(market, features)
            self.assertTrue(updated["ok"])
            self.assertEqual(updated["dataset_version"], 2)
            status = precompute_status(market, features)
            self.assertEqual(status["status"], "ready")
            with duckdb.connect(str(features), read_only=True) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT ma_20 FROM daily_core_features WHERE date<? ORDER BY date DESC LIMIT 1",
                        [new_date],
                    ).fetchone()[0],
                    prior_feature,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM daily_core_features WHERE date=?",
                        [new_date],
                    ).fetchone()[0],
                    1,
                )


if __name__ == "__main__":
    unittest.main()
