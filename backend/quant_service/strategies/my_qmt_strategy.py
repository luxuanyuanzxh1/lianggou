import backtrader as bt

STRATEGY_META = {
    "name": "我的本地回测策略",
    "description": "MA5 上穿 MA20 买入，下穿卖出",
    "params": {"fast": 5, "slow": 20},
}

class Strategy(bt.Strategy):
    params = (("fast", 5), ("slow", 20))

    def __init__(self):
        self.fast = bt.indicators.SimpleMovingAverage(self.data.close, period=self.p.fast)
        self.slow = bt.indicators.SimpleMovingAverage(self.data.close, period=self.p.slow)
        self.cross = bt.indicators.CrossOver(self.fast, self.slow)

    def next(self):
        if not self.position and self.cross[0] > 0:
            self.buy()
        elif self.position and self.cross[0] < 0:
            self.close()
