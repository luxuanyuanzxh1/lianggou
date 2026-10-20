from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
import math
import os, threading
from pathlib import Path
import backtrader as bt
import duckdb, pandas as pd
from .strategy_loader import get_strategy
from .full_market_portfolio import run_full_market_portfolio
from .order_metrics import enrich_order_pnl
from .strategy_risk import (
    HARD_BOARD_LOT,
    HARD_MAX_POSITION_PCT,
    HARD_MAX_TOTAL_EXPOSURE,
)

ROOT=Path(__file__).resolve().parents[1]
DATA_DIR=Path(os.environ.get("QW_DATA_DIR") or (ROOT/"data")).expanduser().resolve()
DB=DATA_DIR/"a_share_eod.duckdb"
UNIFIED_DB=DATA_DIR/"market_data.duckdb"
ADJUSTED_DB=DATA_DIR/"adjusted_kline_cache.duckdb"
PERIOD_TABLE={"日线":"daily_bar","周线":"weekly_bar","月线":"monthly_bar","季线":"quarterly_bar","年线":"yearly_bar"}
ADJUSTMENT_CODE={"前复权":"qfq","后复权":"hfq"}
adjustment_lock=threading.Lock()
ACTIVE_VALUE_FILE=ROOT/"public/data/active-market-value.json"
THS_ACTIVE_PROXY_FILE=ROOT/"public/data/active-market-value-ths-proxy.json"
ACTIVE_GATE_PROFILES={
    "compass":{"start_one":4.0,"start_two":5.0,"exit_one":-2.33,"ma_period":10,"use_two_day":False,"ma_buffer":0.0},
    "ths_internal_proxy":{"start_one":3.75,"start_two":4.95,"exit_one":-2.05,"ma_period":10,"use_two_day":True,"ma_buffer":0.0025},
}
ACTIVE_GATE_FORMAL_START={"ths_internal_proxy":date(2023,1,1)}

class GatePandasData(bt.feeds.PandasData):
    lines = ("active_gate", "industry_trend", "concept_trend")
    params = (("active_gate", -1), ("industry_trend", -1), ("concept_trend", -1))


class AShareBackBroker(bt.brokers.BackBroker):
    """Backtrader broker with conservative A-share price-limit liquidity."""

    @staticmethod
    def _limit_pct(order) -> float:
        try:
            value = float(getattr(order.owner.p, "limit_pct", 0.10))
        except (AttributeError, TypeError, ValueError):
            value = 0.10
        return max(0.0, min(0.30, value))

    def _try_exec_market(self, order, popen, phigh, plow):
        try:
            open_price = float(popen)
            volume = float(order.data.volume[0])
        except (IndexError, TypeError, ValueError):
            return
        if not math.isfinite(open_price) or open_price <= 0:
            if order.isbuy():
                order.cancel()
                self.notify(order)
            return
        if not math.isfinite(volume) or volume <= 0:
            if order.isbuy():
                order.cancel()
                self.notify(order)
            return

        try:
            previous_close = float(order.data.close[-1])
        except (IndexError, TypeError, ValueError):
            previous_close = 0.0
        if math.isfinite(previous_close) and previous_close > 0:
            rate = self._limit_pct(order)
            upper = round(previous_close * (1 + rate) + 1e-10, 2)
            lower = round(previous_close * (1 - rate) + 1e-10, 2)
            if order.isbuy() and open_price >= upper - 0.001:
                # Entry signals are valid for the first T+1 opening only.  A
                # blocked limit-up buy must not turn into an unintended T+2
                # entry after the original signal has gone stale.
                order.cancel()
                self.notify(order)
                return
            if order.issell() and open_price <= lower + 0.001:
                return

        super()._try_exec_market(order, popen, phigh, plow)

def active_gate_series(
    provider="compass",start_one=None,start_two=None,exit_one=None,
    ma_period=None,use_two_day=None,ma_buffer=None,
):
    """生成不含未来函数的活跃市值开关日序列。"""
    if provider not in ACTIVE_GATE_PROFILES: raise ValueError("活跃市值 provider 只支持 compass 或 ths_internal_proxy")
    profile=ACTIVE_GATE_PROFILES[provider]
    start_one=float(profile["start_one"] if start_one is None else start_one)
    start_two=float(profile["start_two"] if start_two is None else start_two)
    exit_one=float(profile["exit_one"] if exit_one is None else exit_one)
    ma_period=max(2,int(profile["ma_period"] if ma_period is None else ma_period))
    use_two_day=bool(profile["use_two_day"] if use_two_day is None else use_two_day)
    ma_buffer=float(profile["ma_buffer"] if ma_buffer is None else ma_buffer)
    source=THS_ACTIVE_PROXY_FILE if provider=="ths_internal_proxy" else ACTIVE_VALUE_FILE
    if not source.exists(): return {}
    payload=json.loads(source.read_text(encoding="utf-8"))
    if provider=="ths_internal_proxy" and (
        payload.get("provider")!="ths_internal_proxy"
        or payload.get("quality")!="close-only"
        or payload.get("not_compass") is not True
    ): raise ValueError("同花顺拟合文件缺少独立代理标识")
    if provider=="ths_internal_proxy" and (
        payload.get("status")!="ready" or payload.get("stale") is not False
    ):
        # 缺失或陈旧代理必须关门；不能沿用上一份开门状态继续交易。
        return {}
    rows=sorted(payload.get("candles",[]),key=lambda x:x.get("date", ""))
    closes=[float(x.get("close") or 0) for x in rows];enabled=False;result={}
    for i,row in enumerate(rows):
        one=(closes[i]/closes[i-1]-1)*100 if i>=1 and closes[i-1] else 0
        two=(closes[i]/closes[i-2]-1)*100 if i>=2 and closes[i-2] else 0
        ma=sum(closes[i-ma_period+1:i+1])/ma_period if i>=ma_period-1 else None
        exit_trigger=one<=exit_one or (ma is not None and closes[i]<ma*(1+ma_buffer))
        start_trigger=one>=start_one or (use_two_day and two>=start_two)
        if enabled and exit_trigger: enabled=False
        elif not enabled and start_trigger and not exit_trigger: enabled=True
        result[row["date"]]=1.0 if enabled else 0.0
    return result

def _active_gate_config(params:dict):
    """从请求中原子选择数据源 profile，再应用显式阈值覆盖。"""
    provider=str(params.pop("active_provider","compass"))
    if provider not in ACTIVE_GATE_PROFILES: raise ValueError("活跃市值 provider 只支持 compass 或 ths_internal_proxy")
    config={"provider":provider,**ACTIVE_GATE_PROFILES[provider]}
    aliases={
        "active_start_one":"start_one","active_start_two":"start_two",
        "active_exit_one":"exit_one","active_ma":"ma_period",
        "active_use_two_day":"use_two_day","active_ma_buffer":"ma_buffer",
    }
    for request_key,config_key in aliases.items():
        if request_key in params: config[config_key]=params.pop(request_key)
    config["start_one"]=float(config["start_one"]);config["start_two"]=float(config["start_two"])
    config["exit_one"]=float(config["exit_one"]);config["ma_period"]=max(2,int(config["ma_period"]))
    raw_two_day=config["use_two_day"]
    config["use_two_day"]=(raw_two_day.strip().lower() in {"1","true","yes","on"}) if isinstance(raw_two_day,str) else bool(raw_two_day)
    config["ma_buffer"]=float(config["ma_buffer"])
    return config

def _sector_flags(frame: pd.DataFrame, group_col: str) -> pd.Series:
    """逐板块计算“板块价格同时位于知行线和多空线上方”，再按日期合并。"""
    if frame.empty: return pd.Series(dtype="bool")
    frame=frame.copy();frame["date"]=pd.to_datetime(frame["date"],errors="coerce").dt.date
    parts=[]
    for _,rows in frame.groupby(group_col,sort=False):
        rows=rows.sort_values("date").copy();close=pd.to_numeric(rows["close"],errors="coerce")
        short=close.ewm(span=10,adjust=False,min_periods=10).mean().ewm(span=10,adjust=False,min_periods=10).mean()
        multi=sum(close.rolling(n,min_periods=n).mean() for n in (14,28,57,114))/4
        rows["flag"]=(close>short)&(close>multi);parts.append(rows[["date","flag"]])
    combined=pd.concat(parts,ignore_index=True)
    return combined.groupby("date")["flag"].any()

def sector_trend_series(code: str, start, end):
    """返回股票所属行业和概念的历史趋势加分，不读取目标日之后的数据。"""
    conn=duckdb.connect(str(UNIFIED_DB),read_only=True)
    warmup=(pd.Timestamp(start)-pd.Timedelta(days=240)).date();end_date=pd.Timestamp(end).date()
    try:
        sectors=conn.execute("""SELECT sm.sector_id,sm.name,sm.sector_type,sm.tdx_code
          FROM sector_member m JOIN sector_master sm USING(sector_id)
          WHERE m.code=? AND sm.sector_type IN ('industry','concept')""",[code]).df()
        industries=sectors[sectors.sector_type=="industry"] if not sectors.empty else sectors
        concepts=sectors[sectors.sector_type=="concept"] if not sectors.empty else sectors
        industry_frame=pd.DataFrame(columns=["sector_id","date","close"])
        codes=[str(x) for x in industries.tdx_code.dropna().unique()] if not industries.empty else []
        if codes:
            industry_frame=conn.execute("""SELECT i.code AS sector_id,d.date,d.close
              FROM daily_bar d JOIN instrument_master i USING(instrument_id)
              WHERE i.code IN (SELECT * FROM unnest(?)) AND d.date BETWEEN ? AND ?""",[codes,warmup,end_date]).df()
        concept_frame=pd.DataFrame(columns=["sector_id","date","close"])
        ids=[str(x) for x in concepts.sector_id.unique()] if not concepts.empty else []
        if ids:
            returns=conn.execute("""WITH members AS (
                SELECT sector_id,code,CASE
                  WHEN code LIKE '92%' OR code LIKE '8%' OR code LIKE '4%' THEN 'BJ.'||code
                  WHEN code LIKE '5%' OR code LIKE '6%' OR code LIKE '9%' THEN 'SH.'||code
                  ELSE 'SZ.'||code END instrument_id
                FROM sector_member WHERE sector_id IN (SELECT * FROM unnest(?))
              ), raw AS (
                SELECT m.sector_id,d.date,d.instrument_id,d.close,
                  lag(d.close) OVER(PARTITION BY m.sector_id,d.instrument_id ORDER BY d.date) prev_close
                FROM daily_bar d JOIN members m USING(instrument_id)
                WHERE d.date BETWEEN ? AND ?
              ) SELECT sector_id,date,avg(close/prev_close-1) ret
              FROM raw WHERE close>0 AND prev_close>0 GROUP BY sector_id,date ORDER BY sector_id,date""",[ids,warmup,end_date]).df()
            if not returns.empty:
                returns["close"]=(1+returns["ret"].fillna(0)).groupby(returns["sector_id"]).cumprod()*1000
                concept_frame=returns[["sector_id","date","close"]]
    finally: conn.close()
    return _sector_flags(industry_frame,"sector_id"),_sector_flags(concept_frame,"sector_id"),{
        "industries":industries.name.astype(str).tolist() if not industries.empty else [],
        "concepts":concepts.name.astype(str).tolist() if not concepts.empty else [],
    }

def _adjusted_rows(instrument_id:str,code:str,adjustment:str,period:str,asset_type:str):
    with adjustment_lock:
        conn=duckdb.connect(str(ADJUSTED_DB));conn.execute("CREATE TABLE IF NOT EXISTS adjusted_bar(date DATE,instrument_id VARCHAR,adjustment VARCHAR,open DOUBLE,close DOUBLE,high DOUBLE,low DOUBLE,volume DOUBLE,amount DOUBLE,updated_at TIMESTAMP,PRIMARY KEY(date,instrument_id,adjustment))")
        count,last_update=conn.execute("SELECT count(*),max(updated_at) FROM adjusted_bar WHERE instrument_id=? AND adjustment=?",[instrument_id,adjustment]).fetchone()
        fresh=count and last_update and last_update>=datetime.now()-timedelta(minutes=30)
        if not fresh:
            try:
                for key in ("http_proxy","https_proxy","HTTP_PROXY","HTTPS_PROXY"):os.environ.pop(key,None)
                import akshare as ak
                prefix="bj" if instrument_id.startswith("BJ.") else "sh" if instrument_id.startswith("SH.") else "sz"
                if asset_type=="fund":
                    df=ak.fund_etf_hist_sina(symbol=f"{prefix}{code}")
                    if not df.empty:
                        df["date"]=pd.to_datetime(df["date"],errors="coerce").dt.date;div=ak.fund_etf_dividend_sina(symbol=f"{prefix}{code}");div=div.sort_values("日期") if not div.empty else pd.DataFrame(columns=["日期","累计分红"])
                        previous_cumulative=0.0
                        for event in div.itertuples(index=False):
                            event_date=pd.to_datetime(event[0]).date();cash=float(event[1])-previous_cumulative;previous_cumulative=float(event[1]);prior=df[df["date"]<event_date]
                            if prior.empty or cash<=0:continue
                            prior_close=float(prior.iloc[-1]["close"]);ratio=(prior_close-cash)/prior_close if prior_close>cash else 1.0
                            target=df["date"]<event_date if adjustment=="前复权" else df["date"]>=event_date;factor=ratio if adjustment=="前复权" else 1/ratio
                            df.loc[target,["open","close","high","low"]]=df.loc[target,["open","close","high","low"]]*factor
                else:
                    try:df=ak.stock_zh_a_daily(symbol=f"{prefix}{code}",start_date="19900101",end_date=datetime.now().strftime("%Y%m%d"),adjust=ADJUSTMENT_CODE[adjustment])
                    except Exception:df=ak.stock_zh_a_hist(symbol=code,period="daily",start_date="19900101",end_date=datetime.now().strftime("%Y%m%d"),adjust=ADJUSTMENT_CODE[adjustment])
                if not df.empty:
                    df=df.rename(columns={"日期":"date","开盘":"open","收盘":"close","最高":"high","最低":"low","成交量":"volume","成交额":"amount"});df["date"]=pd.to_datetime(df["date"],errors="coerce").dt.date;df["instrument_id"]=instrument_id;df["adjustment"]=adjustment;df["updated_at"]=datetime.now()
                    for col in ("open","close","high","low","volume","amount"):df[col]=pd.to_numeric(df[col],errors="coerce").fillna(0)
                    # Sina's completed-day series can lag the current TDX bar.
                    # Extend newer raw bars with the most recent adjustment ratio.
                    last_date=df["date"].max();raw=duckdb.connect(str(UNIFIED_DB),read_only=True)
                    ref=raw.execute("SELECT close FROM daily_bar WHERE instrument_id=? AND date=?",[instrument_id,last_date]).fetchone();extra=raw.execute('SELECT date,"open","close",high,low,volume,amount FROM daily_bar WHERE instrument_id=? AND date>? ORDER BY date',[instrument_id,last_date]).df();raw.close()
                    if ref and ref[0] and not extra.empty:
                        factor=float(df.loc[df["date"]==last_date,"close"].iloc[-1])/float(ref[0]);extra[["open","close","high","low"]]=extra[["open","close","high","low"]]*factor;extra["instrument_id"]=instrument_id;extra["adjustment"]=adjustment;extra["updated_at"]=datetime.now();df=pd.concat([df,extra],ignore_index=True)
                    incoming=df[["date","instrument_id","adjustment","open","close","high","low","volume","amount","updated_at"]].dropna(subset=["date"]).drop_duplicates(["date"])
                    conn.execute("DELETE FROM adjusted_bar WHERE instrument_id=? AND adjustment=?",[instrument_id,adjustment]);conn.register("adjusted_incoming",incoming);conn.execute("INSERT INTO adjusted_bar SELECT * FROM adjusted_incoming");conn.unregister("adjusted_incoming")
            except Exception:
                if not count:conn.close();raise
        if period in ("日线","分时"):
            sql='SELECT date,"open","close",high,low,volume,amount FROM adjusted_bar WHERE instrument_id=? AND adjustment=? ORDER BY date'
        else:
            grain={"周线":"week","月线":"month","季线":"quarter","年线":"year"}[period]
            sql=f'''SELECT max(date) date,arg_min("open",date) "open",arg_max("close",date) "close",max(high) high,min(low) low,sum(volume) volume,sum(amount) amount FROM adjusted_bar WHERE instrument_id=? AND adjustment=? GROUP BY date_trunc('{grain}',date) ORDER BY date'''
        rows=conn.execute(sql,[instrument_id,adjustment]).fetchall();conn.close();return rows

def load_kline_rows(market:int,code:str,period:str="日线",adjustment:str="不复权"):
    if not UNIFIED_DB.exists():raise ValueError("全市场 DuckDB 尚未建立")
    exchange={0:"SZ",1:"SH",2:"BJ"}.get(market)
    if not exchange:raise ValueError("不支持的市场编号")
    table=PERIOD_TABLE.get(period)
    if not table:raise ValueError("周期仅支持日线、周线、月线、季线、年线")
    date_col="date" if table=="daily_bar" else "period_end"
    instrument_id=f"{exchange}.{code}";conn=duckdb.connect(str(UNIFIED_DB),read_only=True)
    try:asset=conn.execute("SELECT asset_type FROM instrument_master WHERE instrument_id=?",[instrument_id]).fetchone()
    finally:conn.close()
    if adjustment in ADJUSTMENT_CODE and (not asset or asset[0]!="index"):rows=_adjusted_rows(instrument_id,code,adjustment,period,asset[0] if asset else "stock")
    else:
        conn=duckdb.connect(str(UNIFIED_DB),read_only=True)
        try:rows=conn.execute(f'''SELECT {date_col} AS date,"open","close",high,low,volume,amount FROM {table} WHERE instrument_id=? ORDER BY date''',[instrument_id]).fetchall()
        finally:conn.close()
    return [{"date":r[0].isoformat(),"open":r[1],"close":r[2],"high":r[3],"low":r[4],"volume":r[5],"amount":r[6]} for r in rows]

class OrderAnalyzer(bt.Analyzer):
    def start(self): self.rows=[]
    def notify_order(self, order):
        if order.status==order.Completed:
            self.rows.append({"date":bt.num2date(order.executed.dt).date().isoformat(),"side":"买入" if order.isbuy() else "卖出","price":round(order.executed.price,4),"shares":abs(int(order.executed.size)),"value":round(abs(order.executed.price*order.executed.size),2),"commission":round(order.executed.comm,2),"reason":getattr(order.info,"reason","")})
    def get_analysis(self): return self.rows

class EquityAnalyzer(bt.Analyzer):
    def start(self): self.rows=[]
    def next(self): self.rows.append({"date":self.strategy.data.datetime.date(0).isoformat(),"value":round(self.strategy.broker.getvalue(),2)})
    def get_analysis(self): return self.rows

def load_bars(code:str,start_date:str|None,end_date:str|None):
    exchange="BJ" if code.startswith("920") else "SH" if code.startswith(("5","6","9")) else "SZ";instrument_id=f"{exchange}.{code}";where=["instrument_id=?"];args=[instrument_id]
    if start_date:where.append("date>=?");args.append(start_date)
    if end_date:where.append("date<=?");args.append(end_date)
    df=pd.DataFrame()
    if UNIFIED_DB.exists():
        conn=duckdb.connect(str(UNIFIED_DB),read_only=True);df=conn.execute(f'SELECT date,"open",high,low,"close",volume,amount FROM daily_bar WHERE {" AND ".join(where)} ORDER BY date',args).df();conn.close()
    if df.empty:
        where=["code=?"]+(["date>=?"] if start_date else [])+(["date<=?"] if end_date else []);args=[code]+([start_date] if start_date else [])+([end_date] if end_date else []);conn=duckdb.connect(str(DB),read_only=True);df=conn.execute(f'SELECT date,"open",high,low,"close",volume,amount FROM daily_bar WHERE {" AND ".join(where)} ORDER BY date',args).df();conn.close()
    if df.empty:raise ValueError(f"本地DuckDB没有 {code} 的行情")
    df["date"]=pd.to_datetime(df["date"]);df=df.set_index("date");df["openinterest"]=0
    df.attrs["code"]=code;df.attrs["exchange"]=exchange
    return df

def instrument_name(code: str) -> str:
    exchange="BJ" if code.startswith("920") else "SH" if code.startswith(("5","6","9")) else "SZ"
    conn=duckdb.connect(str(UNIFIED_DB),read_only=True)
    try:
        row=conn.execute("SELECT name FROM instrument_master WHERE instrument_id=?",[f"{exchange}.{code}"]).fetchone()
    finally: conn.close()
    return str(row[0]) if row and row[0] else ""

def _is_st_name(name: str) -> bool:
    return "ST" in str(name or "").upper()

def run_backtest(req:dict):
    strategy_cls,meta=get_strategy(req["strategy_id"])
    if meta.get("engine")=="portfolio-replay":
        path=ROOT/str(meta.get("portfolio_data"));payload=json.loads(path.read_text(encoding="utf-8"))
        return {**payload,"strategy":meta,"code":"PORTFOLIO","filters":{"exclude_st":True,"exclude_limit_up":True},"sector_context":{"industries":[],"concepts":[]}}
    if meta.get("engine")=="full-market-portfolio":
        return {**run_full_market_portfolio(req,meta),"strategy":meta,"filters":{"exclude_st":True,"exclude_limit_up":True},"sector_context":{"industries":[],"concepts":[]}}
    df=load_bars(req["code"],req.get("start_date"),req.get("end_date"))
    name=str(req.get("name") or instrument_name(req["code"]))
    if _is_st_name(name): raise ValueError(f"已剔除 ST 股票：{name or req['code']}")
    strategy_params={**meta.get("params",{}),**req.get("params",{})}
    gate_config=_active_gate_config(strategy_params)
    supported={k for k,_ in strategy_cls.params._getitems()}
    if "max_position_pct" in supported:
        strategy_params["max_position_pct"] = min(
            HARD_MAX_POSITION_PCT,
            max(0.0, float(strategy_params.get("max_position_pct", HARD_MAX_POSITION_PCT))),
        )
    if "max_total_exposure" in supported:
        strategy_params["max_total_exposure"] = min(
            HARD_MAX_TOTAL_EXPOSURE,
            max(0.0, float(strategy_params.get("max_total_exposure", HARD_MAX_TOTAL_EXPOSURE))),
        )
    if "lot_size" in supported:
        requested_lot = max(HARD_BOARD_LOT, int(strategy_params.get("lot_size", HARD_BOARD_LOT)))
        strategy_params["lot_size"] = int(math.ceil(requested_lot / HARD_BOARD_LOT) * HARD_BOARD_LOT)
    if "exclude_limit_up" in supported: strategy_params["exclude_limit_up"]=bool(req.get("exclude_limit_up",True))
    if "limit_pct" in supported:
        exchange=df.attrs.get("exchange", "SZ");code=req["code"];strategy_params["limit_pct"]=.30 if exchange=="BJ" else .20 if code.startswith(("300","688")) else .10
    if "stop_loss" in supported:
        default_stop=strategy_params.get("stop_loss",.05)
        strategy_params["stop_loss"]=float(strategy_params.get("growth_stop",default_stop) if req["code"].startswith(("300","301","688")) else strategy_params.get("main_stop",default_stop))
    gate=active_gate_series(**gate_config)
    formal_gate_start=ACTIVE_GATE_FORMAL_START.get(gate_config["provider"])
    df["active_gate"]=[
        gate.get(idx.date().isoformat(),0.0)
        if formal_gate_start is None or idx.date()>=formal_gate_start
        else 0.0
        for idx in df.index
    ]
    if meta.get("uses_sector_trend"):
        industry_flags,concept_flags,sector_context=sector_trend_series(req["code"],df.index.min(),df.index.max())
    else:
        industry_flags=pd.Series(dtype="bool");concept_flags=pd.Series(dtype="bool");sector_context={"industries":[],"concepts":[]}
    df["industry_trend"]=[1.0 if industry_flags.get(idx.date(),False) else 0.0 for idx in df.index]
    df["concept_trend"]=[1.0 if concept_flags.get(idx.date(),False) else 0.0 for idx in df.index]
    cerebro=bt.Cerebro(stdstats=False);cerebro.setbroker(AShareBackBroker());cerebro.adddata(GatePandasData(dataname=df,open="open",high="high",low="low",close="close",volume="volume",openinterest="openinterest",active_gate="active_gate",industry_trend="industry_trend",concept_trend="concept_trend"));cerebro.addstrategy(strategy_cls,**strategy_params)
    capital=float(req.get("capital",1_000_000));cerebro.broker.setcash(capital);cerebro.broker.setcommission(commission=float(req.get("commission",0.0003)));cerebro.broker.set_slippage_perc(float(req.get("slippage",0.0001)));cerebro.broker.set_coc(False)
    cerebro.addanalyzer(bt.analyzers.DrawDown,_name="drawdown");cerebro.addanalyzer(bt.analyzers.SharpeRatio_A,_name="sharpe",riskfreerate=0.02);cerebro.addanalyzer(bt.analyzers.TradeAnalyzer,_name="trades");cerebro.addanalyzer(bt.analyzers.Returns,_name="returns");cerebro.addanalyzer(OrderAnalyzer,_name="orders");cerebro.addanalyzer(EquityAnalyzer,_name="equity")
    # Short-history instruments use step mode because some indicators have a
    # longer warm-up than the listing history. Established stocks retain the
    # substantially faster vectorized mode.
    result=cerebro.run(runonce=len(df) >= 150)[0];final=float(cerebro.broker.getvalue());dd=result.analyzers.drawdown.get_analysis();ta=result.analyzers.trades.get_analysis();ra=result.analyzers.returns.get_analysis();orders=result.analyzers.orders.get_analysis();equity=result.analyzers.equity.get_analysis();closed=int(ta.get("total",{}).get("closed",0) or 0);won=int(ta.get("won",{}).get("total",0) or 0)
    for order in orders:
        order["code"] = req["code"]
        order["name"] = name
    enrich_order_pnl(orders)
    return {"engine":"Backtrader 1.9.78.123","strategy":meta,"code":req["code"],"name":name,"filters":{"exclude_st":True,"exclude_limit_up":bool(req.get("exclude_limit_up",True)),"limit_up_pct":strategy_params.get("limit_pct"),"stop_loss":strategy_params.get("stop_loss")},"assumptions":{"signal_execution":"T日收盘信号，T+1下一交易日开盘成交；禁止收盘作弊撮合","sector_membership":"使用当前本地行业/概念成分映射回放历史","active_market_gate":{**gate_config,"formal_backtest_start":formal_gate_start.isoformat() if formal_gate_start else None,"missing_data":"fail_closed"}},"sector_context":sector_context,"range":{"start":df.index.min().date().isoformat(),"end":df.index.max().date().isoformat(),"bars":len(df)},"metrics":{"initial":capital,"final":round(final,2),"total_return":round(final/capital-1,6),"annual_return":round(float(ra.get("rnorm100",0))/100,6),"max_drawdown":round(float(dd.get("max",{}).get("drawdown",0))/100,6),"sharpe":round(float(result.analyzers.sharpe.get_analysis().get("sharperatio") or 0),4),"trades":closed,"win_rate":round(won/closed if closed else 0,6)},"equity":equity,"orders":orders}
