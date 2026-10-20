import backtrader as bt

STRATEGY_META = {
    "id": "ma_cross",
    "name": "双均线趋势（可运行示例）",
    "description": "快线上穿慢线买入，下穿卖出；按A股100股整数手执行。",
    "params": {"fast": 10, "slow": 30},
}

class Strategy(bt.Strategy):
    params = (("fast", 10), ("slow", 30), ("exclude_limit_up", True), ("limit_pct", .10))

    def __init__(self):
        fast = bt.indicators.SimpleMovingAverage(self.data.close, period=self.p.fast)
        slow = bt.indicators.SimpleMovingAverage(self.data.close, period=self.p.slow)
        self.cross = bt.indicators.CrossOver(fast, slow)
        self.pending = None

    def next(self):
        if self.pending:
            return
        limit_up = len(self.data) > 1 and self.data.close[-1] > 0 and self.data.close[0] >= self.data.close[-1] * (1 + self.p.limit_pct) - max(.01, self.data.close[-1] * .001)
        if not self.position and self.cross > 0 and not (self.p.exclude_limit_up and limit_up):
            cash = self.broker.getcash()
            size = int(cash * 0.95 / self.data.close[0] / 100) * 100
            if size:
                self.pending = self.buy(size=size)
        elif self.position and self.cross < 0:
            self.pending = self.close()

    def notify_order(self, order):
        if order.status in (order.Completed, order.Canceled, order.Margin, order.Rejected):
            self.pending = None
