"""状态门控与异构多专家模型的日线代理实现。"""
import backtrader as bt

from quant_service.strategy_risk import (
    DEFAULT_EXPOSURE_PARAMS,
    THS_PROXY_GATE_PARAMS,
    active_gate_is_open,
    capped_entry_size,
    latch_full_exit,
    retry_latched_full_exit,
    update_latched_full_exit,
)


STRATEGY_META = {
    "name": "状态门控·多专家融合",
    "description": "同花顺活跃市值代理硬门控；趋势、动量、量价和板块专家只对门控内标的排序；T收盘、T+1执行。",
    "source": "本组策略选择构建_完整版.docx；多专家协同选股系统.docx",
    "category": "电脑策略库",
    "uses_sector_trend": True,
    "params": {
        **THS_PROXY_GATE_PARAMS,
        **DEFAULT_EXPOSURE_PARAMS,
        "entry_score": 4.0, "stop_loss": 0.06, "max_hold": 30, "volume_ratio": 1.2,
    },
}


class Strategy(bt.Strategy):
    params = (
        ("entry_score", 4.0), ("stop_loss", 0.06), ("main_stop", 0.06), ("growth_stop", 0.06),
        ("max_hold", 30), ("volume_ratio", 1.2),
        ("exclude_limit_up", True), ("limit_pct", 0.10),
        ("max_position_pct", 0.10), ("max_total_exposure", 0.80), ("lot_size", 100),
    )

    def __init__(self):
        c, v = self.data.close, self.data.volume
        self.ma10 = bt.indicators.SimpleMovingAverage(c, period=10)
        self.ma20 = bt.indicators.SimpleMovingAverage(c, period=20)
        self.ma60 = bt.indicators.SimpleMovingAverage(c, period=60)
        self.bbi = sum(bt.indicators.SimpleMovingAverage(c, period=n) for n in (3, 6, 12, 24)) / 4
        self.vol20 = bt.indicators.SimpleMovingAverage(v, period=20)
        self.atr14 = bt.indicators.ATR(self.data, period=14)
        self.pending = None
        self._latched_exit_reason = None
        self.entry_price = 0.0
        self.entry_bar = 0

    def _gate(self):
        return active_gate_is_open(self.data)

    def _limit_up(self):
        return self.data.close[0] >= self.data.close[-1] * (1 + self.p.limit_pct) - max(0.01, self.data.close[-1] * 0.001)

    def _score(self):
        c, o, h, l, v = map(float, (self.data.close[0], self.data.open[0], self.data.high[0], self.data.low[0], self.data.volume[0]))
        score = 0.0
        score += 1.5 if c > self.ma20[0] > self.ma60[0] else 0
        score += 1.0 if c > self.bbi[0] and self.ma10[0] > self.ma20[0] else 0
        score += 1.0 if c / self.data.close[-20] - 1 > 0.08 else 0
        score += 1.0 if v > self.vol20[0] * self.p.volume_ratio and c >= o else 0
        score += 0.75 if hasattr(self.data, "industry_trend") and self.data.industry_trend[0] > 0.5 else 0
        score += 0.75 if hasattr(self.data, "concept_trend") and self.data.concept_trend[0] > 0.5 else 0
        score -= 1.0 if self.atr14[0] / c > 0.06 else 0
        score -= 0.75 if h > l and (h - max(o, c)) / (h - l) > 0.55 else 0
        return score

    def next(self):
        gate_open = self._gate()
        if self.position and not gate_open:
            self._latched_exit_reason = "T收盘活跃市值门控关闭，T+1开盘退出"
        if self.pending or len(self) < 120:
            return
        close = float(self.data.close[0])
        if self.position:
            retry = retry_latched_full_exit(self)
            if retry is not None:
                self.pending = retry
            elif close <= self.entry_price * (1 - self.p.stop_loss):
                self.pending = latch_full_exit(self, "T收盘多专家风险止损，T+1开盘退出")
            elif close < self.ma20[0] or len(self) - self.entry_bar >= self.p.max_hold:
                self.pending = latch_full_exit(self, "多专家趋势或时间退出")
            return
        if not gate_open or (self.p.exclude_limit_up and self._limit_up()):
            return
        score = self._score()
        if score < self.p.entry_score:
            return
        size = capped_entry_size(self, close)
        if size:
            self.pending = self.buy(size=size)
            self.pending.addinfo(reason=f"T收盘多专家{score:.2f}分，T+1开盘买入")

    def notify_order(self, order):
        if order.status in (order.Completed, order.Canceled, order.Margin, order.Rejected):
            update_latched_full_exit(self, order)
            if order.status == order.Completed and order.isbuy():
                self.entry_price = float(order.executed.price); self.entry_bar = len(self)
            self.pending = None
