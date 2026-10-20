from __future__ import annotations

import json
import math
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from pathlib import Path

import backtrader as bt
import numpy as np
import pandas as pd

from .config import PRODUCT_WORK_DIR, RUNS_DIR
from .data import load_daily, load_execution_prices, load_tail_window
from .strategies import STRATEGIES


@dataclass(frozen=True)
class BacktestConfig:
    strategy: str
    codes: list[str]
    start: date | str
    end: date | str
    universe_scope: str = "custom"
    initial_cash: float = 1_000_000.0
    execution_mode: str = "next_open"
    minute_frequency: str = "5m"
    entry_time: str = "14:45"
    commission: float = 0.0003
    min_commission: float = 5.0
    transfer_fee: float = 0.00001
    stamp_tax: float = 0.0005
    slippage: float = 0.0005
    lot_size: int = 100
    invest_ratio: float = 0.95
    max_participation: float = 0.10
    params: dict | None = None

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGIES:
            raise ValueError("未知策略")
        if self.universe_scope not in {"custom", "all", "sh_sz"}:
            raise ValueError("未知股票池范围")
        if self.universe_scope == "custom" and not self.codes:
            raise ValueError("股票池不能为空")
        if self.execution_mode not in {"next_open", "next_1445", "same_day_1445"}:
            raise ValueError("未知成交模式")
        if self.execution_mode == "same_day_1445" and self.strategy != "tail_strength_1445":
            raise ValueError("同日 14:45 冻结信号模式目前只用于“14:45 尾盘强度轮动”")
        if self.initial_cash <= 0 or self.lot_size <= 0:
            raise ValueError("初始资金和整手数量必须为正")
        if not 0 < self.max_participation <= 1:
            raise ValueError("成交量参与率必须在 0 到 1 之间")


class AShareCommission(bt.CommInfoBase):
    params = (
        ("commission", 0.0003),
        ("min_commission", 5.0),
        ("transfer_fee", 0.00001),
        ("stamp_tax", 0.0005),
        ("stocklike", True),
        ("commtype", bt.CommInfoBase.COMM_PERC),
        ("percabs", True),
    )

    def _getcommission(self, size, price, pseudoexec):
        value = abs(size) * price
        if not value:
            return 0.0
        commission = max(float(self.p.min_commission), value * float(self.p.commission))
        fee = commission + value * float(self.p.transfer_fee)
        if size < 0:
            fee += value * float(self.p.stamp_tax)
        return fee


class RuipanPandasData(bt.feeds.PandasData):
    lines = ("signal_flag", "score", "prev_close")
    params = (("signal_flag", -1), ("score", -1), ("prev_close", -1))


class RuipanStrategy(bt.Strategy):
    params = (
        ("strategy_id", "buy_hold"),
        ("strategy_params", None),
        ("invest_ratio", 0.95),
        ("lot_size", 100),
        ("ml_scores", None),
    )

    def __init__(self):
        self.strategy_params = dict(self.p.strategy_params or {})
        self.order_rows: list[dict] = []
        self.closed_rows: list[dict] = []
        self.equity_rows: list[dict] = []
        self.cost_ledger: dict[str, dict[str, float]] = {}
        self.order_reasons: dict[int, str] = {}
        self.last_targets: set[str] = set()
        self.last_rebalance = -10_000
        self.has_entered = False
        self.indicators: dict[str, dict] = {}
        strategy_id = self.p.strategy_id
        for data in self.datas:
            indicators = {}
            if strategy_id == "ma_cross":
                indicators["fast"] = bt.indicators.SimpleMovingAverage(data.close, period=int(self.strategy_params.get("fast", 5)))
                indicators["slow"] = bt.indicators.SimpleMovingAverage(data.close, period=int(self.strategy_params.get("slow", 20)))
            elif strategy_id == "rsi":
                indicators["rsi"] = bt.indicators.RSI_Safe(data.close, period=int(self.strategy_params.get("period", 14)))
            elif strategy_id == "turtle":
                indicators["entry"] = bt.indicators.Highest(data.high(-1), period=int(self.strategy_params.get("entry", 20)))
                indicators["exit"] = bt.indicators.Lowest(data.low(-1), period=int(self.strategy_params.get("exit", 10)))
            self.indicators[data._name] = indicators

    def _score_targets(self) -> set[str]:
        strategy_id = self.p.strategy_id
        held = {data._name for data in self.datas if self.getposition(data).size > 0}
        if strategy_id == "buy_hold":
            return {data._name for data in self.datas}
        if strategy_id == "ma_cross":
            return {
                data._name
                for data in self.datas
                if len(data) > int(self.strategy_params.get("slow", 20))
                and self.indicators[data._name]["fast"][0] > self.indicators[data._name]["slow"][0]
            }
        if strategy_id == "rsi":
            targets = set(held)
            buy_level = float(self.strategy_params.get("buy_level", 30))
            sell_level = float(self.strategy_params.get("sell_level", 70))
            for data in self.datas:
                value = float(self.indicators[data._name]["rsi"][0])
                if math.isfinite(value) and value <= buy_level:
                    targets.add(data._name)
                elif math.isfinite(value) and value >= sell_level:
                    targets.discard(data._name)
            return targets
        if strategy_id == "momentum":
            rebalance = int(self.strategy_params.get("rebalance", 5))
            if len(self) - self.last_rebalance < rebalance:
                return self.last_targets
            lookback = int(self.strategy_params.get("lookback", 20))
            top_n = int(self.strategy_params.get("top_n", 3))
            scores = []
            for data in self.datas:
                if len(data) <= lookback or not data.close[-lookback]:
                    continue
                score = data.close[0] / data.close[-lookback] - 1
                if math.isfinite(score) and score > 0:
                    scores.append((float(score), data._name))
            self.last_rebalance = len(self)
            return {name for _, name in sorted(scores, reverse=True)[:top_n]}
        if strategy_id == "turtle":
            targets = set(held)
            for data in self.datas:
                entry_value = float(self.indicators[data._name]["entry"][0])
                exit_value = float(self.indicators[data._name]["exit"][0])
                if math.isfinite(entry_value) and data.close[0] > entry_value:
                    targets.add(data._name)
                elif data._name in held and math.isfinite(exit_value) and data.close[0] < exit_value:
                    targets.discard(data._name)
            return targets
        if strategy_id == "ml_random_forest":
            rebalance = int(self.strategy_params.get("rebalance", 5))
            if len(self) - self.last_rebalance < rebalance:
                return self.last_targets
            date_key = self.datas[0].datetime.date(0).isoformat()
            scores = (self.p.ml_scores or {}).get(date_key, {})
            threshold = float(self.strategy_params.get("threshold", 0.55))
            top_n = int(self.strategy_params.get("top_n", 3))
            ranked = sorted(
                ((float(score), code) for code, score in scores.items() if float(score) >= threshold),
                reverse=True,
            )
            self.last_rebalance = len(self)
            return {code for _, code in ranked[:top_n]}
        if strategy_id == "tail_strength_1445":
            rebalance = int(self.strategy_params.get("rebalance", 1))
            if len(self) - self.last_rebalance < rebalance:
                return self.last_targets
            top_n = int(self.strategy_params.get("top_n", 3))
            threshold = float(self.strategy_params.get("threshold", 0.60))
            ranked = sorted(
                (
                    (float(data.score[0]), data._name)
                    for data in self.datas
                    if math.isfinite(float(data.score[0])) and float(data.score[0]) >= threshold
                ),
                reverse=True,
            )
            self.last_rebalance = len(self)
            return {name for _, name in ranked[:top_n]}
        return set()

    def next(self):
        if int(round(float(self.datas[0].signal_flag[0]))) != 1:
            return
        date_value = self.datas[0].datetime.date(0)
        self.equity_rows.append(
            {"date": date_value, "equity": float(self.broker.getvalue()), "cash": float(self.broker.getcash())}
        )
        previous_targets = set(self.last_targets)
        targets = self._score_targets()
        if self.p.strategy_id == "buy_hold" and self.has_entered:
            return
        if self.has_entered and targets == previous_targets:
            return
        self.last_targets = set(targets)
        data_by_name = {data._name: data for data in self.datas}
        for data in self.datas:
            if self.getposition(data).size > 0 and data._name not in targets:
                order = self.close(data=data)
                if order:
                    self.order_reasons[order.ref] = "策略退出"
        if targets:
            target_value = float(self.broker.getvalue()) * float(self.p.invest_ratio) / len(targets)
            for name in sorted(targets):
                data = data_by_name[name]
                shares = int(target_value / max(float(data.close[0]), 0.01) / int(self.p.lot_size)) * int(self.p.lot_size)
                order = self.order_target_size(data=data, target=max(0, shares))
                if order:
                    self.order_reasons[order.ref] = "策略目标仓位"
        self.has_entered = self.has_entered or bool(targets)

    def notify_order(self, order):
        if order.status != order.Completed:
            return
        side = "买入" if order.isbuy() else "卖出"
        shares = abs(int(order.executed.size))
        price = float(order.executed.price)
        amount = price * shares
        fee = float(order.executed.comm)
        ledger = self.cost_ledger.setdefault(order.data._name, {"shares": 0.0, "avg_cost": 0.0})
        realized_pnl = None
        realized_return = None
        if order.isbuy():
            total_shares = ledger["shares"] + shares
            ledger["avg_cost"] = (
                ledger["shares"] * ledger["avg_cost"] + amount + fee
            ) / total_shares if total_shares else 0.0
            ledger["shares"] = total_shares
        else:
            cost = min(float(shares), ledger["shares"]) * ledger["avg_cost"]
            realized_pnl = amount - fee - cost
            realized_return = realized_pnl / cost if cost else None
            ledger["shares"] = max(0.0, ledger["shares"] - shares)
            if not ledger["shares"]:
                ledger["avg_cost"] = 0.0
        self.order_rows.append(
            {
                "date": bt.num2date(order.executed.dt).date(),
                "code": order.data._name,
                "side": side,
                "price": price,
                "shares": shares,
                "amount": amount,
                "fee": fee,
                "realized_pnl": realized_pnl,
                "realized_return": realized_return,
                "reason": self.order_reasons.pop(order.ref, "策略调仓"),
            }
        )

    def notify_trade(self, trade):
        if trade.isclosed:
            self.closed_rows.append(
                {
                    "code": trade.data._name,
                    "pnl": float(trade.pnlcomm),
                    "gross_pnl": float(trade.pnl),
                    "bars": int(trade.barlen),
                }
            )


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, np.nan))


def _ml_scores(frame: pd.DataFrame, params: dict) -> dict[str, dict[str, float]]:
    try:
        from sklearn.ensemble import RandomForestClassifier
    except ImportError as exc:
        raise RuntimeError("机器学习案例需要 scikit-learn，请运行 npm run backtest:setup") from exc
    train_window = int(params.get("train_window", 250))
    retrain = int(params.get("retrain", 20))
    output: dict[str, dict[str, float]] = {}
    for instrument_id, part in frame.groupby("instrument_id"):
        part = part.sort_values("date").copy()
        close = part["close"]
        features = pd.DataFrame(index=part.index)
        features["ret1"] = close.pct_change()
        features["ret5"] = close.pct_change(5)
        features["ret20"] = close.pct_change(20)
        features["vol20"] = close.pct_change().rolling(20).std()
        features["ma20"] = close / close.rolling(20).mean() - 1
        features["rsi14"] = _rsi(close, 14) / 100
        features["volume_ratio"] = part["volume"] / part["volume"].rolling(20).mean()
        label = (close.shift(-1) > close).astype(int)
        valid = features.notna().all(axis=1)
        last_model = None
        for position in range(len(part)):
            if not valid.iloc[position] or position < 120:
                continue
            if last_model is None or position % max(1, retrain) == 0:
                train_start = max(0, position - train_window)
                train_index = [index for index in range(train_start, position) if valid.iloc[index]]
                if len(train_index) < 100 or label.iloc[train_index].nunique() < 2:
                    continue
                last_model = RandomForestClassifier(
                    n_estimators=120,
                    max_depth=5,
                    min_samples_leaf=8,
                    random_state=42,
                    n_jobs=-1,
                )
                last_model.fit(features.iloc[train_index], label.iloc[train_index])
            score = float(last_model.predict_proba(features.iloc[[position]])[0, 1])
            day = pd.Timestamp(part.iloc[position]["date"]).date().isoformat()
            output.setdefault(day, {})[str(instrument_id)] = score
    return output


def _prepare_tail_frames(config: BacktestConfig, instruments: list[dict]) -> tuple[pd.DataFrame, dict]:
    identifiers = [item["instrument_id"] for item in instruments]
    tail = load_tail_window(identifiers, config.start, config.end, config.minute_frequency, config.entry_time)
    if tail.empty:
        raise ValueError("所选范围没有完整的 14:30–14:50 分钟窗口，请先抓取分钟数据")
    # Previous close is known before today's 14:45 decision and is safe to use.
    warmup_start = pd.Timestamp(config.start) - pd.Timedelta(days=45)
    daily, _ = load_daily(config.codes, warmup_start.date(), config.end)
    daily = daily.sort_values(["instrument_id", "date"])
    daily["prev_close"] = daily.groupby("instrument_id")["close"].shift(1)
    tail = tail.merge(daily[["date", "instrument_id", "prev_close"]], on=["date", "instrument_id"], how="left")
    tail["day_return"] = tail["signal_price"] / tail["prev_close"] - 1
    tail["tail_rank"] = tail.groupby("date")["tail_return"].rank(pct=True)
    tail["day_rank"] = tail.groupby("date")["day_return"].rank(pct=True)
    tail["score"] = tail["tail_rank"] * 0.45 + tail["day_rank"] * 0.55
    available = tail.groupby("instrument_id")["date"].apply(set)
    common_dates = None
    for identifier in identifiers:
        dates = available.get(identifier, set())
        common_dates = dates if common_dates is None else common_dates.intersection(dates)
    common_dates = common_dates or set()
    tail = tail[tail["date"].isin(common_dates)]
    if tail["date"].nunique() < 30:
        raise ValueError("股票池共同拥有的同日 14:45 窗口少于 30 天，请缩小股票池或补数据")
    signal_events = pd.DataFrame(
        {
            "date": tail["date"],
            "feed_datetime": tail["signal_time"],
            "instrument_id": tail["instrument_id"],
            "feed_open": tail["signal_price"],
            "high": tail["signal_price"],
            "low": tail["signal_price"],
            "close": tail["signal_price"],
            "volume": tail["tail_volume"],
            "amount": tail["tail_amount"],
            "signal_flag": 1.0,
            "score": tail["score"],
            "prev_close": tail["prev_close"],
        }
    )
    execution_events = pd.DataFrame(
        {
            "date": tail["date"],
            "feed_datetime": tail["execution_time"],
            "instrument_id": tail["instrument_id"],
            "feed_open": tail["execution_price"],
            "high": tail["execution_high"],
            "low": tail["execution_low"],
            "close": tail["execution_close"],
            "volume": tail["execution_volume"],
            "amount": tail["execution_amount"],
            "signal_flag": 0.0,
            "score": np.nan,
            "prev_close": tail["prev_close"],
        }
    )
    events = pd.concat([signal_events, execution_events], ignore_index=True).sort_values(["feed_datetime", "instrument_id"])
    expected = max(1, len(identifiers) * tail["date"].nunique())
    coverage = {
        "minute_rows": int(len(tail)),
        "minute_coverage": float(len(tail) / expected),
        "same_day_signal": True,
    }
    return events, coverage


def _prepare_frames(config: BacktestConfig):
    daily, instruments = load_daily(config.codes, config.start, config.end)
    if daily.empty:
        raise ValueError("所选范围没有本地日线数据")
    if config.execution_mode == "same_day_1445":
        events, coverage = _prepare_tail_frames(config, instruments)
        return events, instruments, coverage
    minute_coverage = 0.0
    minute_rows = 0
    if config.execution_mode == "next_1445":
        execution = load_execution_prices(
            [item["instrument_id"] for item in instruments],
            config.start,
            config.end,
            config.minute_frequency,
            config.entry_time,
        )
        if execution.empty:
            raise ValueError("所选范围没有 14:45 分钟线，请先在“分钟数据”页抓取")
        minute_rows = len(execution)
        available = execution.groupby("instrument_id")["date"].apply(set)
        common_dates = None
        for item in instruments:
            dates = available.get(item["instrument_id"], set())
            common_dates = dates if common_dates is None else common_dates.intersection(dates)
        common_dates = common_dates or set()
        expected = daily["date"].nunique() * max(1, len(instruments))
        minute_coverage = len(execution) / expected if expected else 0.0
        daily = daily[daily["date"].isin(common_dates)].merge(
            execution[["date", "instrument_id", "execution_price"]],
            on=["date", "instrument_id"],
            how="inner",
        )
        daily["feed_open"] = daily["execution_price"]
        daily["high"] = daily[["high", "feed_open", "close"]].max(axis=1)
        daily["low"] = daily[["low", "feed_open", "close"]].min(axis=1)
        if daily["date"].nunique() < 30:
            raise ValueError("所有股票共同拥有的 14:45 分钟数据少于 30 个交易日，请缩小股票池或补数据")
    else:
        daily["feed_open"] = daily["open"]
    daily = daily.sort_values(["instrument_id", "date"])
    daily["prev_close"] = daily.groupby("instrument_id")["close"].shift(1).fillna(0.0)
    daily["feed_datetime"] = daily["date"] + pd.Timedelta(hours=15)
    daily["signal_flag"] = 1.0
    daily["score"] = np.nan
    return daily, instruments, {"minute_rows": minute_rows, "minute_coverage": minute_coverage}


def _metrics(equity: pd.DataFrame, trades: pd.DataFrame, closed: pd.DataFrame, initial_cash: float) -> dict:
    if equity.empty:
        return {}
    values = equity.set_index("date")["equity"].astype(float)
    values = values[~values.index.duplicated(keep="last")]
    returns = values.pct_change().fillna(0.0)
    total_return = values.iloc[-1] / initial_cash - 1
    years = max((values.index[-1] - values.index[0]).days / 365.25, 1 / 252)
    cagr = (values.iloc[-1] / initial_cash) ** (1 / years) - 1 if values.iloc[-1] > 0 else -1.0
    volatility = returns.std(ddof=0) * math.sqrt(252)
    sharpe = returns.mean() / returns.std(ddof=0) * math.sqrt(252) if returns.std(ddof=0) else 0.0
    drawdown = values / values.cummax() - 1
    winners = closed[closed["pnl"] > 0] if not closed.empty else closed
    turnover = trades["amount"].sum() / values.mean() if not trades.empty and values.mean() else 0.0
    return {
        "total_return": float(total_return),
        "cagr": float(cagr),
        "annual_volatility": float(volatility),
        "sharpe": float(sharpe),
        "max_drawdown": float(drawdown.min()),
        "calmar": float(cagr / abs(drawdown.min())) if drawdown.min() else 0.0,
        "trades": int(len(trades)),
        "closed_trades": int(len(closed)),
        "win_rate": float(len(winners) / len(closed)) if len(closed) else 0.0,
        "turnover": float(turnover),
        "final_equity": float(values.iloc[-1]),
    }


def run_backtest(config: BacktestConfig, persist: bool = True) -> dict:
    daily, instruments, coverage = _prepare_frames(config)
    params = {**STRATEGIES[config.strategy].default_params, **(config.params or {})}
    ml_scores = _ml_scores(daily, params) if config.strategy == "ml_random_forest" else {}
    cerebro = bt.Cerebro(stdstats=False, runonce=True)
    cerebro.broker.setcash(config.initial_cash)
    cerebro.broker.addcommissioninfo(
        AShareCommission(
            commission=config.commission,
            min_commission=config.min_commission,
            transfer_fee=config.transfer_fee,
            stamp_tax=config.stamp_tax,
        )
    )
    metadata = {item["instrument_id"]: item for item in instruments}
    allowed_instruments = {
        key for key, item in metadata.items() if "ST" not in str(item.get("name", "")).upper()
    }
    daily = daily[daily["instrument_id"].isin(allowed_instruments)]
    cerebro.broker.set_slippage_perc(config.slippage, slip_open=True, slip_match=True, slip_out=False)

    def ashare_filler(order, price, ago):
        data = order.data
        remaining = abs(int(order.executed.remsize))
        if remaining <= 0 or float(data.volume[ago]) <= 0:
            return 0
        previous_close = float(data.prev_close[ago]) if len(data) > 1 else 0.0
        item = metadata.get(data._name, {})
        code = str(item.get("code", ""))
        rate = 0.30 if item.get("exchange") == "BJ" else 0.20 if code.startswith(("300", "301", "688")) else 0.10
        if previous_close > 0:
            upper = round(previous_close * (1 + rate) + 1e-8, 2)
            lower = round(previous_close * (1 - rate) + 1e-8, 2)
            if order.isbuy() and price >= upper - 0.001:
                return 0
            if order.issell() and price <= lower + 0.001:
                return 0
        capacity = int(float(data.volume[ago]) * config.max_participation / config.lot_size) * config.lot_size
        return max(0, min(remaining, capacity))

    cerebro.broker.set_filler(ashare_filler)
    usable = []
    for instrument_id, part in daily.groupby("instrument_id"):
        part = part.sort_values("feed_datetime").set_index("feed_datetime")
        if len(part) < 25:
            continue
        feed_frame = pd.DataFrame(
            {
                "open": part["feed_open"],
                "high": part["high"],
                "low": part["low"],
                "close": part["close"],
                "volume": part["volume"].fillna(0),
                "openinterest": 0,
                "signal_flag": part["signal_flag"],
                "score": part["score"],
                "prev_close": part["prev_close"],
            },
            index=part.index,
        )
        cerebro.adddata(RuipanPandasData(dataname=feed_frame), name=str(instrument_id))
        usable.append(str(instrument_id))
    if not usable:
        raise ValueError("所选股票没有足够的共同历史数据")
    cerebro.addstrategy(
        RuipanStrategy,
        strategy_id=config.strategy,
        strategy_params=params,
        invest_ratio=config.invest_ratio,
        lot_size=config.lot_size,
        ml_scores=ml_scores,
    )
    result_strategy = cerebro.run()[0]
    equity = pd.DataFrame(result_strategy.equity_rows)
    trades = pd.DataFrame(result_strategy.order_rows)
    closed = pd.DataFrame(result_strategy.closed_rows)
    if not trades.empty:
        trades["name"] = trades["code"].map(lambda code: metadata.get(code, {}).get("name", code))
        trades["symbol"] = trades.apply(lambda row: f"{row['name']} {metadata.get(row['code'], {}).get('code', row['code'])}", axis=1)
    closes = daily[daily["instrument_id"].isin(usable)].pivot_table(index="date", columns="instrument_id", values="close", aggfunc="last").sort_index()
    benchmark_returns = closes.pct_change().mean(axis=1).fillna(0.0)
    benchmark = (1 + benchmark_returns).cumprod() * config.initial_cash
    if not equity.empty:
        equity["date"] = pd.to_datetime(equity["date"])
        equity = equity.sort_values("date").drop_duplicates("date", keep="last")
        equity["benchmark"] = equity["date"].map(benchmark).ffill().fillna(config.initial_cash)
        equity["drawdown"] = equity["equity"] / equity["equity"].cummax() - 1
    metrics = _metrics(equity, trades, closed, config.initial_cash)
    result = {
        "run_id": datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "config": {**asdict(config), "start": str(config.start), "end": str(config.end), "params": params},
        "strategy": STRATEGIES[config.strategy],
        "metrics": metrics,
        "coverage": {**coverage, "daily_rows": len(daily), "dates": int(daily["date"].nunique()), "instruments": len(usable)},
        "equity": equity,
        "trades": trades,
        "closed_trades": closed,
    }
    if persist:
        persist_result(result)
    return result


def write_result_directory(result: dict, run_dir: Path) -> Path:
    """Write immutable result artifacts to an explicit directory."""
    run_dir.mkdir(parents=True, exist_ok=True)
    result["equity"].to_parquet(run_dir / "equity.parquet", index=False)
    result["trades"].to_parquet(run_dir / "trades.parquet", index=False)
    result["closed_trades"].to_parquet(run_dir / "closed-trades.parquet", index=False)
    optional_frames = {
        "stage_funnel": "stage-funnel.parquet",
        "rank_details": "rank-details.parquet",
        "factor_analysis": "factor-analysis.parquet",
        "factor_groups": "factor-groups.parquet",
        "factor_correlations": "factor-correlations.parquet",
    }
    for key, filename in optional_frames.items():
        frame = result.get(key)
        if isinstance(frame, pd.DataFrame):
            frame.to_parquet(run_dir / filename, index=False)
    metadata = {
        key: value
        for key, value in result.items()
        if key not in {
            "equity", "trades", "closed_trades", "stage_funnel",
            "rank_details", "factor_analysis", "factor_groups",
            "factor_correlations", "strategy",
        }
    }
    strategy = result.get("strategy", {})
    metadata["strategy"] = asdict(strategy) if is_dataclass(strategy) else dict(strategy or {})
    (run_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return run_dir


def persist_result(result: dict) -> Path:
    return write_result_directory(result, RUNS_DIR / result["run_id"])


def persist_result_reference(result: dict, artifact_dir: Path) -> Path:
    """Create an auditable run whose large frames live in the shared cache."""
    run_dir = RUNS_DIR / result["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        key: value
        for key, value in result.items()
        if key not in {
            "equity", "trades", "closed_trades", "stage_funnel",
            "rank_details", "factor_analysis", "factor_groups",
            "factor_correlations", "strategy",
        }
    }
    strategy = result.get("strategy", {})
    metadata["strategy"] = asdict(strategy) if is_dataclass(strategy) else dict(strategy or {})
    metadata["artifact_ref"] = artifact_dir.resolve().relative_to(PRODUCT_WORK_DIR.resolve()).as_posix()
    (run_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return run_dir


def list_runs(limit: int = 30) -> list[dict]:
    if not RUNS_DIR.exists():
        return []
    rows = []
    for path in sorted(RUNS_DIR.glob("*/metadata.json"), reverse=True)[:limit]:
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return rows


def load_result_directory(run_dir: Path) -> dict:
    metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    result = {
        **metadata,
        "equity": pd.read_parquet(run_dir / "equity.parquet"),
        "trades": pd.read_parquet(run_dir / "trades.parquet"),
        "closed_trades": pd.read_parquet(run_dir / "closed-trades.parquet"),
    }
    optional_frames = {
        "stage_funnel": "stage-funnel.parquet",
        "rank_details": "rank-details.parquet",
        "factor_analysis": "factor-analysis.parquet",
        "factor_groups": "factor-groups.parquet",
        "factor_correlations": "factor-correlations.parquet",
    }
    for key, filename in optional_frames.items():
        path = run_dir / filename
        if path.exists():
            result[key] = pd.read_parquet(path)
    return result


def load_run(run_id: str) -> dict:
    run_dir = RUNS_DIR / Path(run_id).name
    alias = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    artifact_ref = alias.get("artifact_ref")
    if not artifact_ref:
        return load_result_directory(run_dir)
    artifact_dir = (PRODUCT_WORK_DIR / str(artifact_ref)).resolve()
    try:
        artifact_dir.relative_to(PRODUCT_WORK_DIR.resolve())
    except ValueError as exc:
        raise ValueError("运行记录包含无效的缓存引用") from exc
    result = load_result_directory(artifact_dir)
    result.update(alias)
    return result
