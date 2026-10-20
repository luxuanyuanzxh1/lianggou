"""B2 多因子量化闭环的单证券 Backtrader 版本。"""
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
    "name": "B2·趋势回调再确认",
    "description": "同花顺活跃市值代理硬门控；结构、趋势、量价确认是硬资格，多因子分只在合格信号内排序；T收盘、T+1执行。",
    "source": "~/Documents/Codex/2026-06-18/new-chat/outputs/B2多因子量化模型",
    "category": "电脑策略库",
    "params": {
        **THS_PROXY_GATE_PARAMS,
        **DEFAULT_EXPOSURE_PARAMS,
        "entry_score": 70.0, "stop_loss": 0.08, "max_hold": 30, "use_active_gate": 1,
    },
}


class Strategy(bt.Strategy):
    params = (
        ("entry_score", 70.0), ("stop_loss", 0.08), ("main_stop", 0.08), ("growth_stop", 0.08),
        ("max_hold", 30), ("use_active_gate", 1),
        ("exclude_limit_up", True), ("limit_pct", 0.10),
        ("max_position_pct", 0.10), ("max_total_exposure", 0.80), ("lot_size", 100),
    )

    def __init__(self):
        c, h, l, v = self.data.close, self.data.high, self.data.low, self.data.volume
        self.ma20 = bt.indicators.SimpleMovingAverage(c, period=20)
        self.ma60 = bt.indicators.SimpleMovingAverage(c, period=60)
        self.vol20 = bt.indicators.SimpleMovingAverage(v, period=20)
        self.atr14 = bt.indicators.ATR(self.data, period=14)
        self.pending = None
        self._latched_exit_reason = None
        self.entry_price = 0.0
        self.entry_bar = 0

    @staticmethod
    def _clip(value, low, high):
        return max(0.0, min(100.0, (value - low) / (high - low) * 100.0))

    @staticmethod
    def _triangle(value, center, width):
        return max(0.0, min(100.0, 100.0 * (1.0 - abs(value - center) / width)))

    def _gate(self):
        # ``use_active_gate`` remains accepted for request compatibility, but
        # this strategy no longer permits callers to bypass the hard gate.
        return active_gate_is_open(self.data)

    def _limit_up(self):
        return self.data.close[0] >= self.data.close[-1] * (1 + self.p.limit_pct) - max(0.01, self.data.close[-1] * 0.001)

    def _score(self):
        c, o, h, l, v = map(float, (self.data.close[0], self.data.open[0], self.data.high[0], self.data.low[0], self.data.volume[0]))
        ret20 = c / self.data.close[-20] - 1
        alignment = 50 * (c > self.ma20[0]) + 50 * (self.ma20[0] > self.ma60[0])
        trend = 0.55 * alignment + 0.45 * self._clip(ret20, -0.05, 0.30)
        impulse = self._clip(self.data.close[-3] / self.data.close[-15] - 1, 0.05, 0.35)
        high15 = max(self.data.high[-i] for i in range(1, 16))
        pullback = self._triangle(max(0.0, 1 - c / high15), 0.07, 0.08)
        structure = 0.40 * impulse + 0.35 * pullback + 0.25 * (100 if c >= self.ma20[0] else 0)
        pullback_volume = sum(self.data.volume[-i] for i in range(1, 4)) / 3 / (self.vol20[0] + 1e-9)
        volume_ratio = v / (self.vol20[0] + 1e-9)
        volume = 0.45 * self._triangle(pullback_volume, 0.65, 0.45) + 0.55 * self._triangle(volume_ratio, 1.6, 1.4)
        high5 = max(self.data.high[-i] for i in range(1, 6))
        reclaim = self._clip(c / high5 - 1, -0.03, 0.05)
        close_position = (c - l) / (h - l + 1e-9)
        confirmation = 0.60 * reclaim + 0.40 * self._clip(close_position, 0.45, 0.95)
        atr_penalty = self._clip(self.atr14[0] / c, 0.03, 0.10)
        shadow_penalty = self._clip((h - max(o, c)) / c, 0.02, 0.12)
        stop_level = min(min(self.data.low[-i] for i in range(5)), self.ma20[0])
        stop_penalty = self._clip(c / stop_level - 1, 0.05, 0.18)
        risk = 0.45 * atr_penalty + 0.25 * shadow_penalty + 0.30 * stop_penalty
        raw = 0.25 * trend + 0.30 * structure + 0.25 * volume + 0.15 * confirmation + 5 - 0.20 * risk
        return max(0.0, min(100.0, raw)), trend, structure, volume, confirmation

    def next(self):
        gate_open = self._gate()
        if self.position and not gate_open:
            self._latched_exit_reason = "T收盘活跃市值门控关闭，T+1开盘退出"
        if self.pending or len(self) < 70:
            return
        close = float(self.data.close[0])
        if self.position:
            retry = retry_latched_full_exit(self)
            if retry is not None:
                self.pending = retry
            elif close <= self.entry_price * (1 - self.p.stop_loss):
                self.pending = latch_full_exit(self, "T收盘B2结构止损，T+1开盘退出")
            elif close < self.ma20[0] or len(self) - self.entry_bar >= self.p.max_hold:
                self.pending = latch_full_exit(self, "B2环境/趋势/时间退出")
            return
        if not gate_open:
            return
        score, trend, structure, volume, confirmation = self._score()
        candidate = trend >= 55 and structure >= 55 and volume >= 50 and confirmation >= 50
        if candidate and score >= self.p.entry_score and not (self.p.exclude_limit_up and self._limit_up()):
            size = capped_entry_size(self, close)
            if size:
                self.pending = self.buy(size=size); self.pending.addinfo(reason=f"T收盘B2硬资格+多因子{score:.1f}分，T+1开盘买入")

    def notify_order(self, order):
        if order.status in (order.Completed, order.Canceled, order.Margin, order.Rejected):
            update_latched_full_exit(self, order)
            if order.status == order.Completed and order.isbuy():
                self.entry_price = float(order.executed.price); self.entry_bar = len(self)
            self.pending = None
