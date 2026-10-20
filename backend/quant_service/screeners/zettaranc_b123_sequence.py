"""Zettaranc B1/B2/B3 sequence screener on Ruipan's local daily bars."""

from quant_service.zettaranc_rules import evaluate_b123


SCREENER_META = {
    "name": "Z·B1/B2/B3·三阶段",
    "description": "按严格日线口径识别B1超卖、B2放量确认与B3缩量中继；同花顺门控和趋势纪律是硬条件，多因子不得绕过。",
    "lookback": 180,
    "params": {
        "use_active_gate": 1,
        "active_provider": "ths_internal_proxy",
        "active_start_one": 3.75,
        "active_start_two": 4.95,
        "active_use_two_day": 1,
        "active_exit_one": -2.05,
        "active_ma": 10,
        "active_ma_buffer": 0.0025,
        "j_threshold": -10.0,
        "green_brick_limit": 4,
        "b1_min_gain": -2.0,
        "b1_max_gain": 1.8,
        "b1_max_amplitude": 7.0,
        "b1_max_volume_ratio": 0.8,
        "require_b1_price_window": 1,
        "require_b1_amplitude": 1,
        "require_shrinking_volume": 1,
        "require_n_lift": 1,
        "forbid_any_green_brick": 1,
        "require_no_s1": 1,
        "require_above_white": 0,
        "require_above_yellow": 0,
        "require_white_above_yellow": 1,
        "require_support": 1,
        "require_macd_positive": 1,
        "b1_min_age": 1,
        "b1_max_age": 3,
        "b2_min_gain": 4.0,
        "b2_max_j": 55.0,
        "b2_min_volume_ratio": 1.2,
        "b3_b2_lookback": 2,
        "b3_max_gain": 2.0,
        "b3_max_amplitude": 7.0,
        "b3_max_volume_ratio": 0.7,
    },
    "columns": ["阶段", "J", "知行白线", "多空黄线", "MACD_DIF", "砖形状态", "N型上移", "参考止损"],
}


def screen(bars, params):
    return evaluate_b123(bars, params)
