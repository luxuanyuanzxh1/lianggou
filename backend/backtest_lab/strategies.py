from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyInfo:
    id: str
    name: str
    category: str
    summary: str
    data_requirement: str
    default_params: dict
    source_name: str
    source_url: str
    lesson: str


STRATEGIES = {
    item.id: item
    for item in [
        StrategyInfo(
            "buy_hold",
            "等权买入持有",
            "基准",
            "首次信号后等权建仓并持有，用来判断复杂策略是否真的增加价值。",
            "日线；若选择 14:45 执行则需要对应日期分钟线。",
            {},
            "Backtrader Quickstart",
            "https://github.com/mementum/backtrader",
            "先和最简单基准比较。策略跑赢买入持有之前，不应因为指标更多就认为更好。",
        ),
        StrategyInfo(
            "ma_cross",
            "双均线趋势",
            "趋势",
            "短均线上穿长均线持有，下穿退出；多只股票之间等权。",
            "至少需要长均线周期加 20 根日线。",
            {"fast": 5, "slow": 20},
            "Backtrader MA Cross examples",
            "https://github.com/mementum/backtrader/tree/master/samples",
            "这是理解信号、仓位、换手和趋势策略震荡损耗的第一课。",
        ),
        StrategyInfo(
            "rsi",
            "RSI 均值回归",
            "反转",
            "RSI 进入超卖区建仓，回到高位退出。",
            "至少需要 RSI 周期的 4 倍日线。",
            {"period": 14, "buy_level": 30, "sell_level": 70},
            "Backtrader RSI indicator",
            "https://github.com/mementum/backtrader",
            "均值回归通常在震荡期有效、单边下跌期危险，必须观察最大回撤和持仓时间。",
        ),
        StrategyInfo(
            "momentum",
            "截面动量轮动",
            "轮动",
            "按过去一段时间收益率排名，定期持有排名靠前且动量为正的股票。",
            "多股票日线，至少需要动量周期加 20 根。",
            {"lookback": 20, "top_n": 3, "rebalance": 5},
            "Quantopian Lectures: Momentum",
            "https://github.com/quantopian/research_public",
            "这是从单票择时走向组合研究的关键案例，重点是横截面排名和调仓成本。",
        ),
        StrategyInfo(
            "turtle",
            "海龟突破",
            "趋势",
            "突破前期高点入场，跌破短期低点退出。",
            "日线高低价，至少需要突破周期加 20 根。",
            {"entry": 20, "exit": 10},
            "Backtrader Turtle sample ecosystem",
            "https://github.com/mementum/backtrader",
            "用它学习突破、假突破、趋势持仓和参数敏感性，不能只挑最优参数。",
        ),
        StrategyInfo(
            "tail_strength_1445",
            "14:45 尾盘强度轮动",
            "尾盘选股",
            "14:45 冻结 14:30 以来的尾盘强度和当日相对涨幅，下一根分钟 K 线开盘成交。",
            "必须有 14:30–14:50 的 1/5/15 分钟线；推荐 5 分钟。",
            {"top_n": 3, "threshold": 0.60, "rebalance": 1},
            "VeighNa PortfolioStrategy event ordering",
            "https://github.com/vnpy/vnpy_portfoliostrategy",
            "这是最贴合 14:45 选股习惯的案例。关键不是收益，而是信号冻结和成交必须错开，避免同一根 K 线未来函数。",
        ),
        StrategyInfo(
            "ml_random_forest",
            "随机森林趋势分类（教学）",
            "机器学习",
            "用收益、波动率、均线距离、RSI 和量比预测下一日方向，只做滚动训练。",
            "建议至少 300 根日线；每个预测仅使用当时已知数据。",
            {"train_window": 250, "retrain": 20, "rebalance": 5, "top_n": 3, "threshold": 0.55},
            "Microsoft Qlib",
            "https://github.com/microsoft/qlib",
            "教学重点不是模型复杂度，而是时间切分、标签泄漏、滚动训练和样本外验证。",
        ),
    ]
}
