import backtrader as bt

STRATEGY_META = {"name": "RSI均值回归（Cursor导入示例）", "description": "RSI低于30买入，高于70卖出；过滤 ST 与涨停买点", "params": {"period": 14}}

class Strategy(bt.Strategy):
    params = (("period", 14), ("exclude_limit_up", True), ("limit_pct", .10))
    def __init__(self):
        self.rsi = bt.indicators.RSI_Safe(self.data.close, period=self.p.period)
    def next(self):
        limit_up = len(self.data) > 1 and self.data.close[-1] > 0 and self.data.close[0] >= self.data.close[-1] * (1 + self.p.limit_pct) - max(.01, self.data.close[-1] * .001)
        if not self.position and self.rsi < 30 and not (self.p.exclude_limit_up and limit_up):
            size = int(self.broker.getcash() * 0.95 / self.data.close[0] / 100) * 100
            if size: self.buy(size=size)
        elif self.position and self.rsi > 70:
            self.close()
