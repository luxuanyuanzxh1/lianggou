"""通达信 B1 多因子评分策略的 Backtrader 实现。

来源：~/Desktop/股票/通达信多因子打分模型版1/b1选股代码.txt。
保留原公式的 KDJ、MACD、趋势、量价、缺口、背离与风险加减分结构。
"""
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
    "name": "B1·KDJ低位多因子评分",
    "description": "同花顺活跃市值代理硬门控；J<13、MACD多头和知行趋势为硬条件，多因子只对合格信号评分；T收盘、T+1执行。",
    "source": "~/Desktop/股票/通达信多因子打分模型版1/b1选股代码.txt",
    "category": "电脑策略库",
    "params": {
        **THS_PROXY_GATE_PARAMS,
        **DEFAULT_EXPOSURE_PARAMS,
        "X": 10.0, "stop_loss": 0.05, "max_hold": 20,
    },
}


class Strategy(bt.Strategy):
    params = (
        ("X", 10.0), ("stop_loss", 0.05), ("main_stop", 0.05), ("growth_stop", 0.07),
        ("max_hold", 20), ("exclude_limit_up", True), ("limit_pct", 0.10),
        ("max_position_pct", 0.10), ("max_total_exposure", 0.80), ("lot_size", 100),
    )

    def __init__(self):
        c, h, l, v = self.data.close, self.data.high, self.data.low, self.data.volume
        lowest9, highest9 = bt.indicators.Lowest(l, period=9), bt.indicators.Highest(h, period=9)
        rsv = 100 * (c - lowest9) / (highest9 - lowest9 + 1e-9)
        self.k = bt.indicators.SmoothedMovingAverage(rsv, period=3)
        self.d = bt.indicators.SmoothedMovingAverage(self.k, period=3)
        self.j = 3 * self.k - 2 * self.d
        self.dif = bt.indicators.ExponentialMovingAverage(c, period=12) - bt.indicators.ExponentialMovingAverage(c, period=26)
        self.dea = bt.indicators.ExponentialMovingAverage(self.dif, period=9)
        self.ma60 = bt.indicators.SimpleMovingAverage(c, period=60)
        self.zx = bt.indicators.ExponentialMovingAverage(bt.indicators.ExponentialMovingAverage(c, period=10), period=10)
        self.multi = sum(bt.indicators.SimpleMovingAverage(c, period=n) for n in (14, 28, 57, 114)) / 4
        self.bbi = sum(bt.indicators.SimpleMovingAverage(c, period=n) for n in (3, 6, 12, 24)) / 4
        delta = c - c(-1)
        up = bt.If(delta > 0, delta, 0.0)
        self.rsi3 = 100 * bt.indicators.SmoothedMovingAverage(up, period=3) / (bt.indicators.SmoothedMovingAverage(abs(delta), period=3) + 1e-9)
        self.vol_ma30 = bt.indicators.SimpleMovingAverage(v, period=30)
        self.pending = None
        self._latched_exit_reason = None
        self.entry_price = 0.0
        self.entry_bar = 0

    @staticmethod
    def _clip(value, low, high):
        return max(low, min(high, value))

    def _vals(self, line, n, start=0):
        return [float(line[-i]) for i in range(start, start + n)]

    def _limit_up(self, ago=0):
        if len(self) < 2 - ago:
            return False
        return self.data.close[ago] >= self.data.close[ago - 1] * (1 + self.p.limit_pct) - max(0.01, self.data.close[ago - 1] * 0.001)

    def _gate(self):
        return active_gate_is_open(self.data)

    def _score(self):
        c, o, h, l, v = map(float, (self.data.close[0], self.data.open[0], self.data.high[0], self.data.low[0], self.data.volume[0]))
        prev = float(self.data.close[-1])
        change = (c / prev - 1) * 100 if prev else 0
        amplitude = (h - l) / prev * 100 if prev else 0
        score = 0.6 if self.dif[0] >= 0 else 0
        score += 0.3 if c > self.ma60[0] else 0
        score += 1.5 if -2 <= change <= 1.8 else -3
        score += 0.5 if amplitude < 7 else -1
        score += 1 if amplitude < 4 else 0
        score += 0.8 if self.rsi3[0] < 20 else 0
        score += 0.7 if self.rsi3[0] < 23 else 0
        score += 1.5 if self.zx[0] > c > self.multi[0] else 0
        score += -3 if c < self.multi[0] else 0
        score += -3 if c * 1.003 < self.multi[0] else 0
        for n, points in ((30, 1.0), (20, 0.8), (15, 0.8), (10, 0.8)):
            score += points if v <= min(self._vals(self.data.volume, n)) * 1.000001 else 0
        if any(v <= min(self._vals(self.data.volume, n)) * 1.000001 for n in (18, 19, 20)):
            score += 0.5
        for n, bull_points, bear_points in ((60, 1.0, -0.5), (30, 0.5, -0.6), (20, 0.4, -0.7), (15, 0.0, -0.8), (10, 0.3, -1.0)):
            vols = self._vals(self.data.volume, n)
            index = max(range(n), key=lambda i: vols[i])
            bullish = self.data.close[-index] >= self.data.open[-index]
            score += bull_points if bullish else bear_points
        for n, pos, neg in ((50, 0.3, -0.3), (30, 0.8, -0.8), (20, 0.5, -0.5)):
            bull = sum(self.data.volume[-i] for i in range(n) if self.data.close[-i] > self.data.open[-i])
            bear = sum(self.data.volume[-i] for i in range(n) if self.data.close[-i] < self.data.open[-i])
            score += pos if bull > bear else neg if bear > bull else 0
            if n == 30 and bear:
                score += 0.4 if bull > bear * 1.25 else 0
                score += 0.5 if bull > bear * 1.5 else 0
                score += 0.6 if bull > bear * 2 else 0
        score += 0.5 if self.bbi[0] > self.bbi[-20] else 0
        pressure = max(self._vals(self.data.high, 10, 1))
        dist_trend, dist_pressure = abs(l - self.multi[0]), abs(c - pressure)
        score += -1 if dist_trend * 2.5 > dist_pressure else 0
        score += -1 if dist_trend * 3 > dist_pressure else 0
        gap = (c - self.zx[0]) / self.zx[0] if self.zx[0] else 0
        c15 = -0.015 < gap <= 0.023 and -2 <= change <= 1.8 and amplitude < 4
        score += 1.5 if c15 else -0.5
        c16 = not c15 and -0.015 < gap <= 0.03 and -2 <= change <= 1.8 and amplitude < 4
        score += 2 if c16 else 0
        multi_gap = (c - self.multi[0]) / self.multi[0] if self.multi[0] else 99
        c17 = not c15 and not c16 and multi_gap <= 0.025 and -2 <= change <= 1.8 and amplitude < 4
        score += 0.5 if c17 else 0
        score += -1.5 if not c15 and not c16 and not c17 and c < self.zx[0] and c > self.multi[0] else 0
        high_pos = max(range(30), key=lambda i: self.data.high[-i])
        if high_pos > 0:
            high_dif = self.dif[-high_pos]
            score += -0.5 if high_dif < max(self._vals(self.dif, 15, high_pos + 1)) else 0
            score += -0.5 if high_dif < max(self._vals(self.dif, 20, high_pos + 1)) else 0
        big_bull = []
        for i in range(20):
            big_bull.append(self.data.close[-i] > self.data.open[-i] and self.data.volume[-i] > self.vol_ma30[-i] * 4)
        if any(big_bull):
            peak_ratio = max(float(self.data.volume[-i] / (self.vol_ma30[-i] + 1e-9)) for i in range(20) if self.data.close[-i] > self.data.open[-i])
            for threshold, points in ((4, 0.5), (4.5, 0.4), (5, 0.3), (5.5, 0.2)):
                score += points if peak_ratio > threshold else 0
        max_range = 0.0
        for offset in range(20):
            hi = max(self._vals(self.data.high, 20, offset))
            lo = min(self._vals(self.data.low, 20, offset))
            max_range = max(max_range, (hi / lo - 1) * 100 if lo else 0)
        for threshold, penalty in ((60, -0.8), (70, -0.7), (80, -0.6), (90, -0.5), (100, -0.4)):
            score += penalty if max_range > threshold else 0
        gaps = sum(1 for i in range(20) if self.data.close[-i] >= self.data.open[-i] and self.data.low[-i] > self.data.high[-i-1] and self.data.low[-i] / self.data.close[-i-1] - 1 > 0.032)
        score += -1.8 if gaps else 0
        score += -1 if gaps and any(big_bull) else 0
        zavg = sum(self.multi[-i] for i in (15, 30, 45, 60)) / 4
        zchange = (self.multi[0] - zavg) / zavg if zavg else 0
        if not any(big_bull):
            score += -1 if zchange < 0.05 else 0
            score += -0.8 if zchange < 0.075 else 0
            score += -0.5 if zchange < 0.10 else 0
        bull20 = sum(self.data.volume[-i] for i in range(20) if self.data.close[-i] > self.data.open[-i])
        old_bull21 = sum(self.data.volume[-i] for i in range(35, 56) if self.data.close[-i] > self.data.open[-i])
        score += 0.5 if bull20 > old_bull21 else -1
        score += -2 if self.zx[0] < self.zx[-1] and self.multi[0] < self.multi[-1] else 0
        dojis = sum(1 for i in range(60) if abs(self.data.close[-i] - self.data.open[-i]) <= 0.01 and not self._limit_up(-i))
        score += -1.5 if dojis > 2 else 0
        y_index = max(range(1, 16), key=lambda i: self.j[-i])
        yc = self.data.close[-y_index]
        if self.j[-y_index] > 95 and yc and abs(c / yc - 1) <= 0.03:
            yrange = (max(self._vals(self.data.high, y_index + 1)) - min(self._vals(self.data.low, y_index + 1))) / yc * 100
            score += 3 if yrange < 12 else 0
        return float(score)

    def next(self):
        gate_open = self._gate()
        if self.position and not gate_open:
            self._latched_exit_reason = "T收盘活跃市值门控关闭，T+1开盘退出"
        if self.pending or len(self) < 140:
            return
        close = float(self.data.close[0])
        if self.position:
            retry = retry_latched_full_exit(self)
            if retry is not None:
                self.pending = retry
            elif close <= self.entry_price * (1 - self.p.stop_loss):
                self.pending = latch_full_exit(self, "T收盘B1固定止损，T+1开盘退出")
            elif close < self.zx[0] or len(self) - self.entry_bar >= self.p.max_hold:
                self.pending = latch_full_exit(self, "B1趋势或时间退出")
            return
        if not gate_open:
            return
        hard_signal = self.j[0] < 13 and self.dif[0] >= 0 and self.zx[0] > self.multi[0]
        if not hard_signal or (self.p.exclude_limit_up and self._limit_up()):
            return
        score = self._score()
        if score < self.p.X:
            return
        size = capped_entry_size(self, close)
        if size:
            self.pending = self.buy(size=size)
            self.pending.addinfo(reason=f"T收盘B1硬信号+多因子{score:.1f}分，T+1开盘买入")

    def notify_order(self, order):
        if order.status in (order.Completed, order.Canceled, order.Margin, order.Rejected):
            update_latched_full_exit(self, order)
            if order.status == order.Completed and order.isbuy():
                self.entry_price = float(order.executed.price); self.entry_bar = len(self)
            self.pending = None
