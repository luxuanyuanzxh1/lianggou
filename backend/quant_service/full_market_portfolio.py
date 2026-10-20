from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from .order_metrics import enrich_order_pnl


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("QW_DATA_DIR") or (ROOT / "data")).expanduser().resolve()
DB = DATA_DIR / "market_data.duckdb"
CACHE = ROOT / "work/full-market-portfolio-cache"


def _limit_rate(code: str) -> float:
    if code.startswith(("300", "301", "688")):
        return 0.20
    if code.startswith(("4", "8", "92")):
        return 0.30
    return 0.10


def _round(value: float, digits: int = 6) -> float:
    return round(float(value), digits) if math.isfinite(float(value)) else 0.0


def _date(value: object) -> str:
    return pd.Timestamp(value).date().isoformat()


def _metrics(equity: pd.DataFrame, orders: list[dict], initial: float) -> dict:
    values = equity["value"].astype(float)
    returns = values.pct_change().dropna()
    drawdown = values / values.cummax() - 1
    total = values.iloc[-1] / initial - 1 if initial else 0
    annual = (1 + total) ** (252 / max(len(returns), 1)) - 1 if total > -1 else -1
    volatility = returns.std(ddof=1) * math.sqrt(252) if len(returns) > 1 else 0
    sharpe = returns.mean() / returns.std(ddof=1) * math.sqrt(252) if len(returns) > 1 and returns.std(ddof=1) else 0
    max_drawdown = abs(float(drawdown.min()))
    sold = [row for row in orders if row["side"] == "卖出"]
    return {
        "initial": _round(initial, 2), "final": _round(values.iloc[-1], 2),
        "total_return": _round(total), "annual_return": _round(annual),
        "max_drawdown": _round(max_drawdown), "sharpe": _round(sharpe, 4),
        "volatility": _round(volatility), "win_rate": _round((returns > 0).mean()),
        "best_day": _round(returns.max() if len(returns) else 0),
        "worst_day": _round(returns.min() if len(returns) else 0),
        "calmar": _round(annual / max_drawdown, 4) if max_drawdown else 0,
        "turnover": _round(sum(row["value"] for row in orders) / max(values.mean(), 1)),
        "trades": len(orders), "rebalances": len({row["date"] for row in orders}),
        "closed_trades": len(sold),
    }


def run_full_market_portfolio(req: dict, meta: dict) -> dict:
    start = pd.Timestamp(req.get("start_date") or "2025-01-01").date()
    end = pd.Timestamp(req.get("end_date") or date.today().isoformat()).date()
    if end < start:
        raise ValueError("回测结束日期不能早于开始日期")
    params = {**meta.get("params", {}), **req.get("params", {})}
    top_n = max(2, min(100, int(params.get("top_n", 10))))
    rebalance_days = max(1, min(20, int(params.get("rebalance_days", 5))))
    capital = float(req.get("capital", 1_000_000))
    commission = float(req.get("commission", 0.0003))
    slippage = float(req.get("slippage", 0.0001))
    participation = max(0.001, min(0.5, float(params.get("max_bar_participation", 0.10))))
    key = hashlib.sha256(json.dumps({"strategy":meta.get("id",meta.get("name","")),"start":str(start),"end":str(end),"capital":capital,"commission":commission,"slippage":slippage,"params":params},sort_keys=True).encode()).hexdigest()[:16]
    cache_file = CACHE / f"{key}.json"
    if cache_file.exists():
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        enrich_order_pnl(cached.get("orders", []))
        return cached

    warmup = start - timedelta(days=45)
    conn = duckdb.connect(str(DB), read_only=True)
    bars = conn.execute(
        """SELECT d.date,d.instrument_id,m.code,m.name,d.open,d.high,d.low,d.close,d.volume,d.amount
           FROM daily_bar d JOIN instrument_master m USING(instrument_id)
           WHERE m.asset_type='stock' AND d.date BETWEEN ? AND ?
             AND d.open>0 AND d.high>0 AND d.low>0 AND d.close>0 AND d.amount>0
             AND upper(coalesce(m.name,'')) NOT LIKE '%ST%'
           ORDER BY d.instrument_id,d.date""",
        [warmup, end],
    ).df()
    conn.close()
    if bars.empty:
        raise ValueError("全市场数据库在所选区间没有股票日线")

    bars["date"] = pd.to_datetime(bars["date"])
    grouped = bars.groupby("instrument_id", sort=False)
    bars["prev_close"] = grouped["close"].shift(1)
    bars["momentum_2"] = bars["close"] / grouped["close"].shift(2) - 1
    bars["momentum_10"] = bars["close"] / grouped["close"].shift(10) - 1
    bars["breakout_20"] = bars["close"] / grouped["high"].transform(lambda x: x.rolling(20, min_periods=20).max())
    spread = bars["high"] - bars["low"]
    bars["close_position"] = ((bars["close"] - bars["low"]) / spread.where(spread > 0)).fillna(0.5)
    ranked = bars[bars["date"].dt.date >= start].copy()
    factor_columns = ["close_position", "momentum_2", "momentum_10", "breakout_20"]
    for column in factor_columns:
        ranked[column + "_rank"] = ranked.groupby("date")[column].rank(pct=True)
    weights = {
        "close_position": float(params.get("close_position_weight", 0.324)),
        "momentum_2": float(params.get("momentum_2_weight", 0.139)),
        "momentum_10": float(params.get("momentum_10_weight", 0.360)),
        "breakout_20": float(params.get("breakout_20_weight", 0.177)),
    }
    total_weight = sum(max(value, 0) for value in weights.values()) or 1
    ranked["score"] = sum(ranked[name + "_rank"].fillna(0) * max(weight, 0) / total_weight for name, weight in weights.items())
    ranked["limit_up"] = ranked.apply(lambda row: bool(row.prev_close and row.close >= row.prev_close * (1 + _limit_rate(str(row.code))) * 0.998), axis=1)
    eligible = ranked.dropna(subset=["momentum_2", "momentum_10", "breakout_20"])
    eligible = eligible[(eligible["volume"] > 0) & (~eligible["limit_up"])]
    signals = {
        day: part.nlargest(top_n, "score")["instrument_id"].astype(str).tolist()
        for day, part in eligible.groupby("date", sort=True)
    }
    by_day = {day: part.set_index("instrument_id") for day, part in ranked.groupby("date", sort=True)}
    trading_days = sorted(by_day)
    cash = capital
    holdings: dict[str, int] = {}
    last_prices: dict[str, float] = {}
    orders: list[dict] = []
    equity_rows: list[dict] = []
    current_targets: list[str] = []
    previous_day = None

    for index, day in enumerate(trading_days):
        frame = by_day[day]
        for instrument, row in frame.iterrows():
            last_prices[str(instrument)] = float(row.close)
        if previous_day is not None and index % rebalance_days == 0:
            current_targets = signals.get(previous_day, [])
            open_nav = cash + sum(shares * float(frame.loc[code, "open"] if code in frame.index else last_prices.get(code, 0)) for code, shares in holdings.items())
            target_value = open_nav / len(current_targets) if current_targets else 0
            universe = list(dict.fromkeys([*holdings.keys(), *current_targets]))
            for instrument in universe:
                if instrument not in holdings or instrument not in frame.index:
                    continue
                row = frame.loc[instrument];price = float(row.open);code = str(row.code)
                desired = target_value if instrument in current_targets else 0
                excess = holdings[instrument] * price - desired
                sell_shares = min(holdings[instrument], max(0, int(excess / price / 100) * 100))
                at_lower = bool(row.prev_close and price <= float(row.prev_close) * (1 - _limit_rate(code)) * 1.002)
                if sell_shares <= 0 or at_lower:
                    continue
                executed = price * (1 - slippage);gross = sell_shares * executed;fee = gross * commission
                cash += gross - fee;holdings[instrument] -= sell_shares
                if holdings[instrument] <= 0: holdings.pop(instrument, None)
                orders.append({"date":_date(day),"code":code,"name":str(row["name"]),"side":"卖出","price":_round(executed,4),"shares":sell_shares,"value":_round(gross,2),"commission":_round(fee,2)})
            for instrument in current_targets:
                if instrument not in frame.index:
                    continue
                row = frame.loc[instrument];price = float(row.open);code = str(row.code)
                at_upper = bool(row.prev_close and price >= float(row.prev_close) * (1 + _limit_rate(code)) * 0.998)
                if at_upper:
                    continue
                owned = holdings.get(instrument, 0) * price
                wanted = max(0, target_value - owned)
                capacity = float(row.amount) * participation
                buy_shares = int(min(wanted, capacity, cash / (1 + commission)) / (price * (1 + slippage)) / 100) * 100
                if buy_shares <= 0:
                    continue
                executed = price * (1 + slippage);gross = buy_shares * executed;fee = gross * commission
                cash -= gross + fee;holdings[instrument] = holdings.get(instrument, 0) + buy_shares
                orders.append({"date":_date(day),"code":code,"name":str(row["name"]),"side":"买入","price":_round(executed,4),"shares":buy_shares,"value":_round(gross,2),"commission":_round(fee,2)})
        value = cash + sum(shares * last_prices.get(code, 0) for code, shares in holdings.items())
        equity_rows.append({"date":_date(day),"value":_round(value,2)})
        previous_day = day

    equity = pd.DataFrame(equity_rows)
    metrics = _metrics(equity, orders, capital)
    candles=[];previous=capital
    traded={}
    for order in orders:traded[order["date"]]=traded.get(order["date"],0)+order["value"]
    for row in equity.itertuples(index=False):
        candles.append({"date":row.date,"open":previous,"high":max(previous,row.value),"low":min(previous,row.value),"close":row.value,"volume":traded.get(row.date,0),"amount":traded.get(row.date,0)});previous=row.value
    enrich_order_pnl(orders)
    payload={
        "id":meta.get("id","quant_full_market_portfolio"),"name":meta["name"],"description":meta["description"],"engine":"Full Market Portfolio Replay 1.0",
        "code":"ALL-A","range":{"start":equity_rows[0]["date"],"end":equity_rows[-1]["date"],"bars":len(equity_rows)},"metrics":metrics,
        "universe":{"size":int(ranked["instrument_id"].nunique()),"scanned_rows":len(ranked),"fixed":False},
        "factor_explanations":[
            {"name":"收盘位置","weight":weights["close_position"]/total_weight,"meaning":"收盘越接近当日最高价，排名越高。"},
            {"name":"2日动量","weight":weights["momentum_2"]/total_weight,"meaning":"衡量最近两个交易日的价格强度。"},
            {"name":"10日动量","weight":weights["momentum_10"]/total_weight,"meaning":"衡量短波段趋势。"},
            {"name":"20日突破","weight":weights["breakout_20"]/total_weight,"meaning":"衡量收盘价接近20日最高价的程度。"},
        ],
        "assumptions":{"signal_execution":"当日收盘形成横截面排名，下一调仓日开盘执行","portfolio":f"全市场扫描、Top{top_n}等权、每{rebalance_days}个交易日调仓","constraints":["ST过滤","涨停不买","跌停不卖","100股整手",f"成交额{participation:.0%}容量上限"],"warning":"全市场库缺少历史市值和15分钟数据，因此这是日线适配版，不等同于原始150股票比赛策略。"},
        "equity":equity_rows,"candles":candles,"orders":orders,"parameters":params,"benchmarks":[],
    }
    CACHE.mkdir(parents=True,exist_ok=True);temporary=cache_file.with_suffix(".tmp");temporary.write_text(json.dumps(payload,ensure_ascii=False,separators=(",",":")),encoding="utf-8");temporary.replace(cache_file)
    return payload
