STRATEGY_META = {
    "name": "全A股·日线多因子组合",
    "description": "扫描本地全部A股，以收盘位置、2/10日动量和20日突破进行横截面排名，次日开盘按组合调仓。",
    "category": "组合策略",
    "engine": "full-market-portfolio",
    "read_only": True,
    "params": {
        "top_n": 10,
        "rebalance_days": 5,
        "close_position_weight": 0.324,
        "momentum_2_weight": 0.139,
        "momentum_10_weight": 0.360,
        "breakout_20_weight": 0.177,
        "max_bar_participation": 0.10,
    },
    "source": "睿盘全市场DuckDB日线适配版",
}
