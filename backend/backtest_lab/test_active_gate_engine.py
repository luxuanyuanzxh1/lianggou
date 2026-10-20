from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import quant_service.engine as engine


class ActiveGateEngineTests(unittest.TestCase):
    @staticmethod
    def _write(path: Path, closes: list[float], *, proxy: bool = False) -> None:
        payload = {
            "candles": [
                {"date": f"2026-01-{index:02d}", "close": close}
                for index, close in enumerate(closes, start=1)
            ]
        }
        if proxy:
            payload.update({
                "provider": "ths_internal_proxy",
                "quality": "close-only",
                "not_compass": True,
                "status": "ready",
                "stale": False,
            })
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_rebound_below_ma_does_not_open_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "compass.json"
            self._write(source, [200.0, 200.0, 100.0, 104.1])
            with patch.object(engine, "ACTIVE_VALUE_FILE", source):
                states = engine.active_gate_series(
                    provider="compass", start_one=4.0, start_two=5.0,
                    exit_one=-2.33, ma_period=3, use_two_day=False,
                    ma_buffer=0.0,
                )

        self.assertEqual(states["2026-01-04"], 0.0)

    def test_compass_default_does_not_enable_two_day_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "compass.json"
            self._write(source, [100.0, 102.5, 105.1])
            with patch.object(engine, "ACTIVE_VALUE_FILE", source):
                states = engine.active_gate_series(provider="compass")

        self.assertEqual(states["2026-01-03"], 0.0)

    def test_compass_and_proxy_match_with_identical_candles_and_profile(self) -> None:
        closes = [100.0, 100.0, 104.1, 103.0, 106.9, 102.0]
        profile = {
            "start_one": 3.75, "start_two": 4.95, "exit_one": -2.05,
            "ma_period": 3, "use_two_day": True, "ma_buffer": 0.0025,
        }
        with tempfile.TemporaryDirectory() as directory:
            compass_source = Path(directory) / "compass.json"
            proxy_source = Path(directory) / "proxy.json"
            self._write(compass_source, closes)
            self._write(proxy_source, closes, proxy=True)
            with (
                patch.object(engine, "ACTIVE_VALUE_FILE", compass_source),
                patch.object(engine, "THS_ACTIVE_PROXY_FILE", proxy_source),
            ):
                compass = engine.active_gate_series(provider="compass", **profile)
                proxy = engine.active_gate_series(provider="ths_internal_proxy", **profile)

        self.assertEqual(compass, proxy)

    def test_proxy_provider_selects_complete_default_profile(self) -> None:
        params = {"active_provider": "ths_internal_proxy", "entry_score": 4.0}

        config = engine._active_gate_config(params)

        self.assertEqual(config, {
            "provider": "ths_internal_proxy",
            "start_one": 3.75,
            "start_two": 4.95,
            "exit_one": -2.05,
            "ma_period": 10,
            "use_two_day": True,
            "ma_buffer": 0.0025,
        })
        self.assertEqual(params, {"entry_score": 4.0})


if __name__ == "__main__":
    unittest.main()
