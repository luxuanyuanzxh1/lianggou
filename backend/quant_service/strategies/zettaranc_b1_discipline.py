"""Daily-bar approximation of the Zettaranc B1 discipline.

The upstream source describes intraday exits around 09:33/09:37.  Ruipan does
not pretend daily bars can reproduce those timestamps; this strategy executes
signals on the next trading day's open and labels that limitation in metadata.
"""

import math

import backtrader as bt


STRATEGY_META = {
    "name": "Z·B1·同花顺门控（日线）",
    "description": "同花顺门控后的严格B1：J≤-10、缩量窄幅、N型上移、非绿砖、双线与MACD确认；T日收盘信号在T+1开盘执行。",
    "source": "docs/ZETTARANC-INTEGRATION.md",
    "category": "Z·纪律实验室",
    "params": {
        "use_active_gate": 1,
        "active_provider": "ths_internal_proxy",
        "active_start_one": 3.75,
        "active_start_two": 4.95,
        "active_use_two_day": True,
        "active_exit_one": -2.05,
        "active_ma": 10,
        "active_ma_buffer": 0.0025,
        "j_threshold": -10.0,
        "green_brick_limit": 4,
        "b1_min_gain": -2.0,
        "b1_max_gain": 1.8,
        "b1_max_volume_ratio": 0.8,
        "stop_loss": 0.05,
        "max_hold": 10,
        "no_progress_days": 3,
        "position_pct": 0.10,
    },
}


class ZettarancKDJ(bt.Indicator):
    lines = ("k", "d", "j")
    params = (("period", 9),)

    def __init__(self):
        self.addminperiod(self.p.period)

    def next(self):
        lows = self.data.low.get(size=self.p.period)
        highs = self.data.high.get(size=self.p.period)
        low_value = min(lows)
        high_value = max(highs)
        rsv = 50.0 if high_value == low_value else (self.data.close[0] - low_value) / (high_value - low_value) * 100.0
        previous_k = 50.0 if len(self) == self.p.period or not math.isfinite(float(self.lines.k[-1])) else float(self.lines.k[-1])
        previous_d = 50.0 if len(self) == self.p.period or not math.isfinite(float(self.lines.d[-1])) else float(self.lines.d[-1])
        current_k = (2.0 * previous_k + rsv) / 3.0
        current_d = (2.0 * previous_d + current_k) / 3.0
        self.lines.k[0] = current_k
        self.lines.d[0] = current_d
        self.lines.j[0] = 3.0 * current_k - 2.0 * current_d


class ZettarancBrick(bt.Indicator):
    """Four-brick recurrence with neutral warm-up/equal-value handling."""

    lines = ("brick", "state", "green_count", "var2", "var4", "var5")
    params = (("period", 4),)

    def __init__(self):
        self.addminperiod(self.p.period)

    @staticmethod
    def _previous(line, fallback):
        try:
            value = float(line[-1])
        except (IndexError, TypeError, ValueError):
            return fallback
        return value if math.isfinite(value) else fallback

    def next(self):
        lows = self.data.low.get(size=self.p.period)
        highs = self.data.high.get(size=self.p.period)
        low_value = min(lows)
        high_value = max(highs)
        spread = high_value - low_value
        high_ratio = 0.5 if spread == 0 else (high_value - self.data.close[0]) / spread
        low_ratio = 0.5 if spread == 0 else (self.data.close[0] - low_value) / spread
        raw2 = high_ratio * 100.0 - 90.0
        raw3 = low_ratio * 100.0
        previous2 = self._previous(self.lines.var2, raw2)
        current2 = (raw2 + 3.0 * previous2) / 4.0
        previous4 = self._previous(self.lines.var4, raw3)
        current4 = (raw3 + 5.0 * previous4) / 6.0
        previous5 = self._previous(self.lines.var5, current4)
        current5 = (current4 + 5.0 * previous5) / 6.0
        brick = max(current5 + 100.0 - (current2 + 100.0) - 4.0, 0.0)
        previous_brick = self._previous(self.lines.brick, brick)
        state = 1.0 if brick > previous_brick + 1e-12 else -1.0 if brick < previous_brick - 1e-12 else 0.0
        previous_green = self._previous(self.lines.green_count, 0.0)
        self.lines.var2[0] = current2
        self.lines.var4[0] = current4
        self.lines.var5[0] = current5
        self.lines.brick[0] = brick
        self.lines.state[0] = state
        self.lines.green_count[0] = previous_green + 1.0 if state < 0 else 0.0


class Strategy(bt.Strategy):
    params = (
        ("use_active_gate", 1),
        ("j_threshold", -10.0),
        ("green_brick_limit", 4),
        ("b1_min_gain", -2.0),
        ("b1_max_gain", 1.8),
        ("b1_max_volume_ratio", 0.8),
        ("require_above_white", False),
        ("require_above_yellow", False),
        ("require_white_above_yellow", True),
        ("require_support", True),
        ("require_macd_positive", True),
        ("require_shrinking_volume", True),
        ("require_n_lift", True),
        ("forbid_any_green_brick", True),
        ("require_no_s1", True),
        ("stop_loss", 0.05),
        ("max_hold", 10),
        ("no_progress_days", 3),
        ("position_pct", 0.10),
        ("exit_below_yellow", True),
        ("exclude_limit_up", True),
        ("limit_pct", 0.10),
    )

    def __init__(self):
        close = self.data.close
        ema10 = bt.indicators.ExponentialMovingAverage(close, period=10)
        self.white = bt.indicators.ExponentialMovingAverage(ema10, period=10)
        self.yellow = sum(bt.indicators.SimpleMovingAverage(close, period=n) for n in (14, 28, 57, 114)) / 4.0
        self.ma60 = bt.indicators.SimpleMovingAverage(close, period=60)
        self.dif = bt.indicators.ExponentialMovingAverage(close, period=12) - bt.indicators.ExponentialMovingAverage(close, period=26)
        self.kdj = ZettarancKDJ(self.data)
        self.brick = ZettarancBrick(self.data)
        self.pending = None
        self.entry_price = 0.0
        self.entry_bar = 0
        self.signal_low = 0.0

    def _gate(self):
        return not self.p.use_active_gate or (
            hasattr(self.data, "active_gate") and self.data.active_gate[0] > 0.5
        )

    def _limit_up(self):
        if len(self) < 2 or not self.data.close[-1]:
            return False
        return self.data.close[0] >= self.data.close[-1] * (1 + self.p.limit_pct) - max(0.01, self.data.close[-1] * 0.001)

    def _daily_features(self):
        previous_close = float(self.data.close[-1])
        previous_volume = float(self.data.volume[-1])
        change = (float(self.data.close[0]) / previous_close - 1.0) * 100.0 if previous_close else 0.0
        amplitude = (
            (float(self.data.high[0]) - float(self.data.low[0])) / previous_close * 100.0
            if previous_close else 0.0
        )
        volume_ratio = float(self.data.volume[0]) / previous_volume if previous_volume else float("inf")
        return change, amplitude, volume_ratio

    def _n_lift(self):
        lows = [float(value) for value in self.data.low.get(size=20)]
        return len(lows) == 20 and min(lows[-10:]) >= min(lows[:10]) * 0.98

    def _s1(self, change, volume_ratio):
        return (
            float(self.data.close[0]) < float(self.data.open[0])
            and change <= -3.0
            and volume_ratio >= 1.5
        )

    def next(self):
        if self.pending or len(self) < 114:
            return
        close = float(self.data.close[0])
        change, amplitude, volume_ratio = self._daily_features()
        if self.position:
            reference_stop = max(self.signal_low, self.entry_price * (1.0 - self.p.stop_loss))
            if not self._gate():
                self.pending = self.close(); self.pending.addinfo(reason="活跃市值门控关闭")
            elif close <= reference_stop:
                self.pending = self.close(); self.pending.addinfo(reason="B1入场K线/固定止损")
            elif self.p.require_no_s1 and self._s1(change, volume_ratio):
                self.pending = self.close(); self.pending.addinfo(reason="S1放量大阴线")
            elif self.brick.state[0] < 0:
                self.pending = self.close(); self.pending.addinfo(reason="红砖翻绿")
            elif self.white[0] < self.yellow[0]:
                self.pending = self.close(); self.pending.addinfo(reason="知行白线死叉多空黄线")
            elif self.p.exit_below_yellow and close < self.yellow[0]:
                self.pending = self.close(); self.pending.addinfo(reason="收盘跌破多空黄线")
            elif len(self) - self.entry_bar >= self.p.no_progress_days and close <= self.entry_price * 1.02:
                self.pending = self.close(); self.pending.addinfo(reason="B1三日未脱离成本")
            elif len(self) - self.entry_bar >= self.p.max_hold:
                self.pending = self.close(); self.pending.addinfo(reason="B1日线最长持仓到期")
            return

        amplitude_limit = 7.0 if self.p.limit_pct >= 0.20 else 4.0
        context_ok = (
            (not self.p.require_above_white or close > self.white[0])
            and (not self.p.require_above_yellow or close > self.yellow[0])
            and (not self.p.require_white_above_yellow or self.white[0] >= self.yellow[0])
            and (not self.p.require_support or close >= self.ma60[0] or close >= self.yellow[0])
            and (not self.p.require_macd_positive or self.dif[0] >= 0)
            and (not self.p.require_shrinking_volume or volume_ratio <= self.p.b1_max_volume_ratio)
            and (not self.p.require_n_lift or self._n_lift())
            and (not self.p.forbid_any_green_brick or self.brick.state[0] >= 0)
            and (not self.p.require_no_s1 or not self._s1(change, volume_ratio))
        )
        signal = (
            self._gate()
            and self.kdj.j[0] <= self.p.j_threshold
            and self.p.b1_min_gain <= change <= self.p.b1_max_gain
            and amplitude < amplitude_limit
            and self.brick.green_count[0] < self.p.green_brick_limit
            and context_ok
        )
        if signal and not (self.p.exclude_limit_up and self._limit_up()):
            target_value = min(
                self.broker.getcash(),
                self.broker.getvalue() * max(0.0, min(float(self.p.position_pct), 0.10)),
            )
            size = int(target_value / close / 100) * 100
            if size:
                self.signal_low = float(self.data.low[0])
                self.pending = self.buy(size=size)
                self.pending.addinfo(reason=f"Z·B1 J={self.kdj.j[0]:.2f}")

    def notify_order(self, order):
        if order.status in (order.Completed, order.Canceled, order.Margin, order.Rejected):
            if order.status == order.Completed and order.isbuy():
                self.entry_price = float(order.executed.price)
                self.entry_bar = len(self)
            self.pending = None
