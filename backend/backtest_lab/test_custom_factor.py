from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import duckdb
import pandas as pd

import backtest_lab.lego_pipeline as lego_pipeline
import quant_service.screener_engine as screener_engine
from backtest_lab.lego_pipeline import (
    BLOCK_CATALOG,
    BlockSpec,
    _attach_active_market_gate,
    _compile_custom_factor,
    _compile_custom_formula,
    _active_market_gate_frame,
    _factor_research,
    default_pipeline,
    validate_custom_formula,
    validate_pipeline,
)
from scripts.update_ths_active_proxy import (
    INDEX_WEIGHTS,
    INTERCEPT,
    build_payload as build_ths_proxy_payload,
    build_candles as build_ths_proxy_candles,
    predict_return as predict_ths_proxy_return,
    read_model_inputs as read_ths_proxy_inputs,
)


class CustomFactorFormulaTests(unittest.TestCase):
    def test_compiles_allowed_fields_and_rolling_functions(self) -> None:
        sql, requirements = _compile_custom_factor(
            "MOM(20) + close / MA(10) - VOL(30) + RSI(14)"
        )

        self.assertIn("lag_close_20", sql)
        self.assertIn("ma_10", sql)
        self.assertIn("vol_30", sql)
        self.assertIn("rsi_14", sql)
        self.assertEqual(requirements["momentum"], {20})
        self.assertEqual(requirements["ma_periods"], {10})
        self.assertEqual(requirements["vols"], {30})
        self.assertEqual(requirements["rsis"], {14})

    def test_rejects_arbitrary_python(self) -> None:
        with self.assertRaisesRegex(ValueError, "只允许数值常量|不支持函数"):
            _compile_custom_factor("__import__('os')")

    def test_compiles_boolean_filter_formula(self) -> None:
        sql, requirements = _compile_custom_formula(
            "close > MA(20) and MOM(5) > 0", boolean=True
        )

        self.assertIn("AND", sql)
        self.assertIn("ma_20", sql)
        self.assertIn("lag_close_5", sql)
        self.assertEqual(requirements["ma_periods"], {20})
        self.assertEqual(requirements["momentum"], {5})

    def test_public_formula_validation_reports_mode_and_requirements(self) -> None:
        result = validate_custom_formula(
            "close > MA(20) and MOM(5) > 0", "filter_custom_formula"
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "condition")
        self.assertEqual(result["requirements"]["ma_periods"], [20])
        self.assertEqual(result["requirements"]["momentum"], [5])

        with self.assertRaisesRegex(ValueError, "不支持字段"):
            validate_custom_formula("C > MA(20)", "filter_custom_formula")

    def test_pipeline_accepts_custom_factor(self) -> None:
        pipeline = asdict(default_pipeline())
        normalizer_index = next(
            index for index, block in enumerate(pipeline["blocks"])
            if block["type"] == "normalize_cross_section"
        )
        pipeline["blocks"].insert(normalizer_index, {
            "id": "factor-custom-test",
            "type": "factor_custom_formula",
            "category": "factor",
            "name": "量价强度",
            "enabled": True,
            "combine": "AND",
            "params": {
                "name": "量价强度",
                "formula": "MOM(20) / (VOL(20) + 0.001)",
                "weight": 0.2,
                "direction": "higher",
                "method": "robust_sigmoid",
            },
        })

        validated = validate_pipeline(pipeline)
        custom = next(block for block in validated.blocks if block.type == "factor_custom_formula")
        self.assertEqual(custom.name, "量价强度")

    def test_custom_filter_template_persists_and_updates(self) -> None:
        block = {
            "type": "filter_custom_formula",
            "category": "filter",
            "name": "站上20日线",
            "params": {"group": "custom_filter", "name": "站上20日线", "formula": "close > MA(20)"},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custom-blocks.json"
            with patch.object(lego_pipeline, "CUSTOM_BLOCKS_FILE", path):
                saved = lego_pipeline.save_custom_block_template(block)
                template_id = saved["template"]["id"]
                self.assertIn(f"user_template_{template_id}", lego_pipeline.catalog_with_custom_templates())
                block["name"] = "站上30日线"
                block["params"]["formula"] = "close > MA(30)"
                lego_pipeline.save_custom_block_template(block, template_id)
                rows = lego_pipeline.list_custom_block_templates()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["name"], "站上30日线")

    def test_active_market_gate_exits_on_ma_or_loss(self) -> None:
        base_dates = [f"2026-01-{day:02d}" for day in range(1, 15)]
        ma_exit_closes = [100.0] * 9 + [104.1, 103.0, 102.0, 101.0, 100.0]
        loss_exit_closes = [100.0] * 9 + [104.1, 101.5, 101.6, 101.7, 101.8]
        block = BlockSpec(
            id="active-gate", type="filter_active_market_gate", category="filter",
            params={"start_one": 4.0, "use_two_day": False, "start_two": 5.0,
                    "exit_one": -2.33, "ma_period": 10},
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "active.json"
            with patch.object(lego_pipeline, "ACTIVE_VALUE_FILE", path):
                path.write_text(json.dumps({"candles": [
                    {"date": day, "close": close}
                    for day, close in zip(base_dates, ma_exit_closes)
                ]}), encoding="utf-8")
                ma_frame = _active_market_gate_frame(block)
                ma_exit = ma_frame[ma_frame["active_gate_event"] == "结束"].iloc[0]
                self.assertGreater(ma_exit["active_one_day"], -2.33)
                self.assertLess(ma_exit["active_close"], ma_exit["active_ma"])
                self.assertIn("跌破MA10", ma_exit["active_gate_reason"])

                path.write_text(json.dumps({"candles": [
                    {"date": day, "close": close}
                    for day, close in zip(base_dates, loss_exit_closes)
                ]}), encoding="utf-8")
                loss_frame = _active_market_gate_frame(block)
                loss_exit = loss_frame[loss_frame["active_gate_event"] == "结束"].iloc[0]
                self.assertLessEqual(loss_exit["active_one_day"], -2.33)
                self.assertIn("单日涨幅≤-2.33%", loss_exit["active_gate_reason"])

    def test_active_market_gate_exit_has_priority_over_rebound_open(self) -> None:
        block = BlockSpec(
            id="active-gate", type="filter_active_market_gate", category="filter",
            params={"start_one": 4.0, "use_two_day": False, "start_two": 5.0,
                    "exit_one": -2.33, "ma_period": 3, "ma_buffer": 0.0},
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "active.json"
            path.write_text(json.dumps({"candles": [
                {"date": f"2026-03-0{index}", "close": close}
                for index, close in enumerate([200.0, 200.0, 100.0, 104.1], start=1)
            ]}), encoding="utf-8")
            with patch.object(lego_pipeline, "ACTIVE_VALUE_FILE", path):
                frame = _active_market_gate_frame(block)

        last = frame.iloc[-1]
        self.assertGreaterEqual(last["active_one_day"], 4.0)
        self.assertLess(last["active_close"], last["active_ma_threshold"])
        self.assertFalse(bool(last["active_market_gate"]))
        self.assertEqual(last["active_gate_event"], "")

    def test_active_market_gate_closes_uncovered_dates_instead_of_forward_filling(self) -> None:
        block = BlockSpec(
            id="active-gate", type="filter_active_market_gate", category="filter",
            params={"provider": "compass", "start_one": 4.0,
                    "use_two_day": False, "exit_one": -2.33,
                    "ma_period": 3, "max_gap_days": 0},
        )
        payload = {"candles": [
            {"date": f"2026-06-0{index}", "close": close}
            for index, close in enumerate([100.0, 100.0, 100.0, 104.1], start=1)
        ]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "active.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(lego_pipeline, "ACTIVE_VALUE_FILE", path):
                with duckdb.connect(":memory:") as connection:
                    connection.execute("CREATE TEMP TABLE lego_features(date DATE)")
                    connection.execute(
                        "INSERT INTO lego_features VALUES (DATE '2026-06-04'), (DATE '2026-06-05')"
                    )
                    info = _attach_active_market_gate(
                        connection, block, date(2026, 6, 4), date(2026, 6, 5)
                    )
                    rows = connection.execute(
                        "SELECT date, active_market_gate, active_gate_reason "
                        "FROM lego_features ORDER BY date"
                    ).fetchall()

        self.assertTrue(rows[0][1])
        self.assertFalse(rows[1][1])
        self.assertIn("不沿用旧值", rows[1][2])
        self.assertEqual(info["gap_closed_dates"], 1)
        self.assertEqual(info["exit_signal_dates"], ["2026-06-05"])

    def test_ths_proxy_gate_uses_buffer_and_exports_exit_signal_dates(self) -> None:
        dates = pd.date_range("2026-04-01", periods=4, freq="D")
        payload = {
            "provider": "ths_internal_proxy", "quality": "close-only",
            "not_compass": True, "stale": False,
            "validation": {"gate_state_agreement": 0.9404},
            "candles": [
                {"date": day.date().isoformat(), "close": close}
                for day, close in zip(dates, [100.0, 100.0, 104.0, 102.2])
            ],
        }
        block = BlockSpec(
            id="ths-proxy-gate", type="filter_ths_active_market_proxy_gate", category="filter",
            params={"provider": "ths_internal_proxy", "start_one": 3.75,
                    "use_two_day": True, "start_two": 4.95, "exit_one": -2.05,
                    "ma_period": 3, "ma_buffer": 0.0025, "max_gap_days": 0},
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proxy.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(lego_pipeline, "THS_ACTIVE_PROXY_FILE", path):
                frame = _active_market_gate_frame(block)
                exit_row = frame[frame["active_gate_event"] == "结束"].iloc[0]
                self.assertGreater(exit_row["active_close"], exit_row["active_ma"])
                self.assertLess(exit_row["active_close"], exit_row["active_ma_threshold"])
                self.assertIn("MA3×1.0025", exit_row["active_gate_reason"])

                with duckdb.connect(":memory:") as connection:
                    connection.register("feature_dates", pd.DataFrame({"date": dates.date}))
                    connection.execute("CREATE TEMP TABLE lego_features AS SELECT * FROM feature_dates")
                    info = _attach_active_market_gate(
                        connection, block, dates[0].date(), dates[-1].date()
                    )

        self.assertEqual(info["provider"], "ths_internal_proxy")
        self.assertEqual(info["quality"], "close-only")
        self.assertEqual(info["exit_signal_dates"], ["2026-04-04"])
        self.assertEqual(info["thresholds"]["ma_buffer"], 0.0025)

    def test_gate_data_rejects_duplicates_and_proxy_prevalidation_backtests(self) -> None:
        compass_block = BlockSpec(
            id="active-gate", type="filter_active_market_gate", category="filter",
            params={"start_one": 4.0, "use_two_day": False, "start_two": 5.0,
                    "exit_one": -2.33, "ma_period": 3, "max_gap_days": 0},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compass_path = root / "active.json"
            compass_path.write_text(json.dumps({"candles": [
                {"date": "2026-01-02", "close": 100.0},
                {"date": "2026-01-02", "close": 101.0},
            ]}), encoding="utf-8")
            with patch.object(lego_pipeline, "ACTIVE_VALUE_FILE", compass_path):
                with self.assertRaisesRegex(ValueError, "重复日期"):
                    _active_market_gate_frame(compass_block)

            proxy_path = root / "proxy.json"
            proxy_path.write_text(json.dumps({
                "provider": "ths_internal_proxy", "quality": "close-only",
                "not_compass": True, "formal_backtest_start": "2023-01-01",
                "candles": [
                    {"date": f"2022-12-{day:02d}", "close": 100.0 + day}
                    for day in range(1, 11)
                ],
            }), encoding="utf-8")
            proxy_block = BlockSpec(
                id="ths-proxy", type="filter_ths_active_market_proxy_gate", category="filter",
                params={"provider": "ths_internal_proxy", "start_one": 3.75,
                        "use_two_day": True, "start_two": 4.95, "exit_one": -2.05,
                        "ma_period": 10, "ma_buffer": 0.0025, "max_gap_days": 0},
            )
            with patch.object(lego_pipeline, "THS_ACTIVE_PROXY_FILE", proxy_path):
                with duckdb.connect(":memory:") as connection:
                    connection.execute("CREATE TEMP TABLE lego_features(date DATE)")
                    connection.execute("INSERT INTO lego_features VALUES (DATE '2022-12-30')")
                    with self.assertRaisesRegex(ValueError, "正式样本外回测不得早于"):
                        _attach_active_market_gate(
                            connection, proxy_block, date(2022, 12, 30), date(2022, 12, 30)
                        )

    def test_ths_proxy_catalog_and_formula_are_reproducible(self) -> None:
        defaults = BLOCK_CATALOG["filter_ths_active_market_proxy_gate"]["params"]
        self.assertEqual(defaults["provider"], "ths_internal_proxy")
        self.assertTrue(defaults["use_two_day"])
        self.assertEqual(defaults["max_gap_days"], 0)
        self.assertEqual(defaults["ma_buffer"], 0.0025)

        model_row = {
            "date": "2026-05-06", "amount": 100.0,
            "amount_ma20": 100.0, "previous_amount": 100.0, "breadth": 0.0,
            **{instrument_id: 0.0 for instrument_id in INDEX_WEIGHTS},
        }
        self.assertAlmostEqual(predict_ths_proxy_return(model_row), INTERCEPT)
        candles = build_ths_proxy_candles([
            model_row,
            {**model_row, "date": "2026-05-07"},
        ])
        self.assertEqual(len(candles), 2)
        self.assertAlmostEqual(candles[1]["one_day"], INTERCEPT, places=5)

    def test_ths_proxy_calendar_audit_rejects_an_intermediate_index_gap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "market.duckdb"
            dates = list(pd.bdate_range("2023-01-02", periods=30))
            missing_day = dates[23].date()
            with duckdb.connect(str(database)) as connection:
                connection.execute(
                    "CREATE TABLE instrument_master("
                    "instrument_id VARCHAR, asset_type VARCHAR, exchange VARCHAR)"
                )
                connection.execute(
                    "CREATE TABLE daily_bar("
                    "date DATE, instrument_id VARCHAR, open DOUBLE, high DOUBLE, "
                    "low DOUBLE, close DOUBLE, amount DOUBLE)"
                )
                stocks = ["SH.600001", "SZ.000001", "SZ.300001"]
                connection.executemany(
                    "INSERT INTO instrument_master VALUES (?, 'stock', ?)",
                    [(code, code[:2]) for code in stocks],
                )
                connection.executemany(
                    "INSERT INTO instrument_master VALUES (?, 'index', ?)",
                    [(code, code[:2]) for code in INDEX_WEIGHTS],
                )
                bars = []
                for offset, timestamp in enumerate(dates):
                    day = timestamp.date()
                    for stock_offset, code in enumerate(stocks):
                        close = 10.0 + stock_offset + offset * 0.01
                        bars.append((day, code, close, close, close, close, 1_000_000.0))
                    for index_offset, code in enumerate(INDEX_WEIGHTS):
                        if code == "SH.000852" and day == missing_day:
                            continue
                        close = 1_000.0 + index_offset * 100 + offset
                        bars.append((day, code, close, close, close, close, 0.0))
                connection.executemany(
                    "INSERT INTO daily_bar VALUES (?, ?, ?, ?, ?, ?, ?)", bars
                )

            rows, latest, integrity = read_ths_proxy_inputs(database)
            self.assertEqual(latest, dates[-1].date())
            self.assertFalse(integrity["complete"])
            self.assertEqual(integrity["missing_count"], 1)
            self.assertEqual(integrity["missing_dates"][0]["date"], missing_day.isoformat())
            self.assertIn(
                "index_bar_missing:SH.000852",
                integrity["missing_dates"][0]["reasons"],
            )
            payload = build_ths_proxy_payload(
                rows,
                latest,
                database=database,
                calendar_integrity=integrity,
                generated_at=datetime(2023, 2, 10, 14, 0),
            )
            self.assertTrue(payload["stale"])
            self.assertEqual(payload["status"], "stale")

    def test_ths_proxy_marks_previous_close_stale_after_market_close(self) -> None:
        row = {
            "date": "2026-07-17",
            "amount": 100.0,
            "amount_ma20": 100.0,
            "previous_amount": 100.0,
            "breadth": 0.0,
            **{instrument_id: 0.0 for instrument_id in INDEX_WEIGHTS},
        }
        payload = build_ths_proxy_payload(
            [row],
            date(2026, 7, 17),
            database=Path("market.duckdb"),
            generated_at=datetime(2026, 7, 20, 16, 0),
        )
        self.assertTrue(payload["market_close_missing"])
        self.assertTrue(payload["stale"])

    def test_screener_gate_also_gives_exit_priority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "active.json"
            path.write_text(json.dumps({"candles": [
                {"date": f"2026-06-0{index}", "close": close}
                for index, close in enumerate([200.0, 200.0, 100.0, 104.1], start=1)
            ]}), encoding="utf-8")
            with patch.object(screener_engine, "ACTIVE_VALUE_FILE", path):
                state = screener_engine.active_market_gate(
                    date(2026, 6, 4), ma_period=3
                )
        self.assertFalse(state["enabled"])
        self.assertIsNone(state["last_event"])
        self.assertEqual(state["provider"], "compass")

    def test_screener_proxy_refuses_stale_asof_open(self) -> None:
        payload = {
            "provider": "ths_internal_proxy", "quality": "close-only",
            "not_compass": True, "stale": False,
            "candles": [
                {"date": f"2026-06-{index:02d}", "close": close}
                for index, close in enumerate([100.0, 100.0, 100.0, 104.1], start=1)
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proxy.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(screener_engine, "THS_ACTIVE_PROXY_FILE", path):
                covered = screener_engine.active_market_gate(
                    date(2026, 6, 4), ma_period=3,
                    provider="ths_internal_proxy",
                )
                uncovered = screener_engine.active_market_gate(
                    date(2026, 6, 5), ma_period=3,
                    provider="ths_internal_proxy",
                )
        self.assertTrue(covered["enabled"])
        self.assertFalse(uncovered["enabled"])
        self.assertEqual(uncovered["source_date"], "2026-06-04")
        self.assertIn("不能沿用陈旧值", uncovered["reason"])

    def test_screener_compass_also_refuses_to_forward_fill_an_open_gate(self) -> None:
        payload = {
            "candles": [
                {"date": f"2026-06-{index:02d}", "close": close}
                for index, close in enumerate([100.0, 100.0, 100.0, 104.1], start=1)
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "compass.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(screener_engine, "ACTIVE_VALUE_FILE", path):
                covered = screener_engine.active_market_gate(
                    date(2026, 6, 4), ma_period=3,
                )
                uncovered = screener_engine.active_market_gate(
                    date(2026, 6, 5), ma_period=3,
                )

        self.assertTrue(covered["enabled"])
        self.assertFalse(uncovered["enabled"])
        self.assertEqual(uncovered["source_date"], "2026-06-04")
        self.assertIn("不能沿用旧值", uncovered["reason"])

    def test_active_market_gate_legacy_or_is_normalized_to_and(self) -> None:
        pipeline = asdict(default_pipeline())
        strategy_index = next(
            index for index, block in enumerate(pipeline["blocks"])
            if block["category"] == "strategy"
        )
        pipeline["blocks"].insert(strategy_index, {
            "id": "active-market-gate-legacy-or",
            "type": "filter_active_market_gate",
            "category": "filter",
            "name": "活跃市值门控",
            "enabled": True,
            "combine": "OR",
            "params": {"group": "active_market_gate"},
        })
        pipeline["workflow"] = {}

        validated = validate_pipeline(pipeline)
        normalized_gate = next(
            block for block in validated.blocks
            if block.type == "filter_active_market_gate"
        )
        self.assertEqual(normalized_gate.combine, "AND")

    def test_legacy_screener_gate_defaults_to_single_day_start(self) -> None:
        closes = [100.0] * 8 + [103.0, 106.1]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "active.json"
            path.write_text(json.dumps({"candles": [
                {"date": f"2026-02-{index:02d}", "close": close}
                for index, close in enumerate(closes, start=1)
            ]}), encoding="utf-8")
            with patch.object(screener_engine, "ACTIVE_VALUE_FILE", path):
                single_day = screener_engine.active_market_gate(date(2026, 2, 10))
                optional_two_day = screener_engine.active_market_gate(
                    date(2026, 2, 10), use_two_day=True
                )
        self.assertFalse(single_day["enabled"])
        self.assertTrue(optional_two_day["enabled"])

    def test_blank_workflow_can_be_saved_and_replayed_by_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(lego_pipeline, "PIPELINES_DIR", root / "pipelines"),
                patch.object(lego_pipeline, "PIPELINE_VERSIONS_DIR", root / "versions"),
            ):
                draft = asdict(lego_pipeline.blank_pipeline())
                draft["name"] = "空白研究草稿"
                first = lego_pipeline.save_pipeline(draft, draft["name"])
                self.assertEqual(first["revision"], 1)
                self.assertFalse(first["runnable"])

                draft["description"] = "第二次保存"
                second = lego_pipeline.save_pipeline(draft, draft["name"])
                self.assertEqual(second["revision"], 2)
                versions = lego_pipeline.list_pipeline_versions(draft["id"])
                self.assertEqual([item["revision"] for item in versions], [2, 1])
                restored = lego_pipeline.load_pipeline_version(draft["id"], 1)
                self.assertEqual(restored.description, "")

    def test_factor_research_reports_ic_groups_and_correlations(self) -> None:
        dates = pd.date_range("2026-01-01", periods=55, freq="D")
        daily_rows = []
        ranked_rows = []
        for instrument in range(10):
            instrument_id = f"stock-{instrument}"
            growth = 1.0 + instrument * 0.0008
            for day_index, day in enumerate(dates):
                daily_rows.append({
                    "instrument_id": instrument_id,
                    "date": day.date(),
                    "close": 100.0 * growth ** day_index,
                })
                if day_index < 30:
                    ranked_rows.append({
                        "instrument_id": instrument_id,
                        "date": day.date(),
                        "b0_raw": float(instrument),
                        "b0_score": float(instrument),
                        "b1_raw": float(instrument) * 2.0,
                        "b1_score": float(instrument) * 2.0,
                    })
        descriptors = [
            {
                "block": BlockSpec(
                    id="factor-a", type="factor_custom_formula", category="factor",
                    name="增长因子", params={"direction": "higher", "method": "percentile"},
                ),
                "raw": "b0_raw", "score": "b0_score", "weight": 0.5,
            },
            {
                "block": BlockSpec(
                    id="factor-b", type="factor_custom_formula", category="factor",
                    name="同向因子", params={"direction": "higher", "method": "percentile"},
                ),
                "raw": "b1_raw", "score": "b1_score", "weight": 0.5,
            },
        ]
        with duckdb.connect(":memory:") as connection:
            connection.register("daily_frame", pd.DataFrame(daily_rows))
            connection.register("ranked_frame", pd.DataFrame(ranked_rows))
            connection.execute("CREATE TABLE daily_bar AS SELECT * FROM daily_frame")
            connection.execute("CREATE TEMP TABLE lego_ranked AS SELECT * FROM ranked_frame")
            summary, groups, correlations = _factor_research(
                connection, descriptors, dates[0].date(), dates[29].date()
            )
        self.assertEqual(len(summary), 2)
        self.assertEqual(len(groups), 10)
        self.assertEqual(len(correlations), 1)
        self.assertGreater(float(summary.iloc[0]["rank_ic_20"]), 0.9)
        self.assertGreater(float(summary.iloc[0]["spread_20"]), 0)
        self.assertGreater(float(correlations.iloc[0]["correlation"]), 0.99)


if __name__ == "__main__":
    unittest.main()
