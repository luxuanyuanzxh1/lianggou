from __future__ import annotations
from datetime import date
import json
import re
from pathlib import Path
import duckdb, pandas as pd
from .engine import UNIFIED_DB
from .screener_loader import get_screener

ACTIVE_VALUE_FILE=Path(__file__).resolve().parents[1]/"public/data/active-market-value.json"
THS_ACTIVE_PROXY_FILE=Path(__file__).resolve().parents[1]/"public/data/active-market-value-ths-proxy.json"

def _is_st(name: str) -> bool:
    return bool(re.search(r"(?:^|[^A-Z])\*?ST", str(name or "").upper()))

def _limit_ratio(exchange: str, code: str) -> float:
    if exchange == "BJ" or str(code).startswith(("8", "4")): return .30
    if str(code).startswith(("300", "688")): return .20
    return .10

def _is_limit_up(last, previous) -> bool:
    if previous is None or not previous.close or not last.close: return False
    ratio=_limit_ratio(str(last.exchange),str(last.code))
    return float(last.close) >= float(previous.close)*(1+ratio)-max(.01,float(previous.close)*.001)

def active_market_gate(
    target:date,
    start_one:float=4.0,
    start_two:float=5.0,
    exit_one:float=-2.33,
    ma_period:int=10,
    use_two_day:bool=False,
    *,
    provider:str="compass",
    ma_buffer:float=0.0,
):
    """Replay the active-market switch through target without look-ahead."""
    source=THS_ACTIVE_PROXY_FILE if provider=="ths_internal_proxy" else ACTIVE_VALUE_FILE
    if provider not in {"compass","ths_internal_proxy"}:
        raise ValueError("active market provider 只支持 compass 或 ths_internal_proxy")
    if not source.exists(): return {"enabled":False,"reason":"活跃市值数据文件不存在","date":target.isoformat(),"provider":provider}
    payload=json.loads(source.read_text(encoding="utf-8"))
    if provider=="ths_internal_proxy" and (
        payload.get("provider")!="ths_internal_proxy"
        or payload.get("quality")!="close-only"
        or payload.get("not_compass") is not True
    ):
        raise ValueError("同花顺拟合文件缺少独立代理标识")
    candles=payload.get("candles",[])
    rows=sorted((x for x in candles if x.get("date")<=target.isoformat()),key=lambda x:x["date"])
    latest_date=rows[-1].get("date") if rows else None
    source_stale=(
        bool(payload.get("stale",False))
        or bool(rows and rows[-1].get("stale",False))
    ) if provider=="ths_internal_proxy" else False
    if latest_date!=target.isoformat() or source_stale:
        return {
            "enabled":False,
            "reason":(
                "同花顺代理数据未覆盖目标交易日，不能沿用陈旧值开启门控"
                if provider=="ths_internal_proxy"
                else "指南针活跃市值未覆盖目标交易日，不能沿用旧值开启门控"
            ),
            "date":target.isoformat(),
            "source_date":latest_date,
            "provider":provider,
            "quality":payload.get("quality","ohlcv" if provider=="compass" else None),
            "stale":source_stale,
        }
    if len(rows)<max(ma_period,3): return {"enabled":False,"reason":"活跃市值历史不足","date":target.isoformat()}
    closes=pd.Series([float(x.get("close") or 0) for x in rows],dtype="float64")
    ma=closes.rolling(ma_period,min_periods=ma_period).mean();enabled=False;last_event=None;last_one=0.0;last_two=0.0
    for i,row in enumerate(rows):
        one=(closes.iat[i]/closes.iat[i-1]-1)*100 if i>=1 and closes.iat[i-1] else 0.0
        two=(closes.iat[i]/closes.iat[i-2]-1)*100 if i>=2 and closes.iat[i-2] else 0.0
        last_one,last_two=one,two
        moving_average=float(ma.iat[i]) if pd.notna(ma.iat[i]) else None
        ma_threshold=moving_average*(1+ma_buffer) if moving_average is not None else None
        exit_by_loss=one<=exit_one
        exit_by_ma=ma_threshold is not None and closes.iat[i]<ma_threshold
        exit_trigger=exit_by_loss or exit_by_ma
        start_trigger=one>=start_one or (use_two_day and two>=start_two)
        if enabled and exit_trigger:
            reasons=[]
            if exit_by_loss: reasons.append(f"单日涨幅≤{exit_one:g}%")
            if exit_by_ma:
                buffer_label=f"×{1+ma_buffer:g}" if ma_buffer else ""
                reasons.append(f"跌破MA{ma_period}{buffer_label}")
            enabled=False;last_event={"type":"结束","date":row["date"],"原因":" 或 ".join(reasons),"单日涨幅":round(one,2),"收盘":round(float(closes.iat[i]),2),f"MA{ma_period}":round(moving_average,2) if moving_average is not None else None}
        elif not enabled and start_trigger and not exit_trigger:
            enabled=True;last_event={"type":"开启","date":row["date"],"单日涨幅":round(one,2),"两日涨幅":round(two,2)}
    return {"enabled":enabled,"date":rows[-1]["date"],"单日涨幅":round(last_one,2),"两日涨幅":round(last_two,2),"收盘":round(float(closes.iat[-1]),2),"MA10":round(float(ma.iat[-1]),2) if pd.notna(ma.iat[-1]) else None,"last_event":last_event,"provider":provider,"quality":payload.get("quality","ohlcv" if provider=="compass" else None),"stale":bool(payload.get("stale",False)),"thresholds":{"开启单日":start_one,"启用两日":use_two_day,"开启两日":start_two,"结束单日":exit_one,"结束MA":ma_period,"均线缓冲":ma_buffer}}

def run_screen(req:dict):
    fn,meta=get_screener(req["strategy_id"]);target=pd.to_datetime(req["date"]).date();lookback=max(30,min(int(meta.get("lookback",120)),500));limit=max(1,min(int(req.get("limit",200)),500));universe=req.get("universe","沪深京")
    exchanges={"沪深京":["SH","SZ","BJ"],"沪深":["SH","SZ"],"北交所":["BJ"]}.get(universe,["SH","SZ","BJ"])
    conn=duckdb.connect(str(UNIFIED_DB),read_only=True)
    df=conn.execute('''WITH ranked AS (
        SELECT b.date,b.instrument_id,b."open",b."close",b.high,b.low,b.volume,b.amount,m.code,m.name,m.exchange,
        row_number() OVER(PARTITION BY b.instrument_id ORDER BY b.date DESC) rn
        FROM daily_bar b JOIN instrument_master m USING(instrument_id)
        WHERE m.asset_type='stock' AND m.exchange IN (SELECT * FROM unnest(?)) AND b.date<=?
      ) SELECT * EXCLUDE(rn) FROM ranked WHERE rn<=? ORDER BY instrument_id,date''',[exchanges,target,lookback]).df();conn.close()
    if df.empty:raise ValueError("目标日期之前没有可用的本地股票行情")
    params={**meta.get("params",{}),**req.get("params",{})}
    raw_use_gate=params.get("use_active_gate",False)
    use_gate=(raw_use_gate.strip().lower() in {"1","true","yes","on"}) if isinstance(raw_use_gate,str) else bool(raw_use_gate)
    if use_gate:
        params["_active_market_gate"]=active_market_gate(target,float(params.get("active_start_one",4)),float(params.get("active_start_two",5)),float(params.get("active_exit_one",-2.33)),int(params.get("active_ma",10)),bool(params.get("active_use_two_day",False)),provider=str(params.get("active_provider","compass")),ma_buffer=float(params.get("active_ma_buffer",0.0)))
    results=[];excluded_st=0;excluded_limit_up=0
    for instrument_id,bars in df.groupby("instrument_id",sort=False):
        last=bars.iloc[-1];prev=bars.iloc[-2] if len(bars)>1 else None
        if _is_st(last["name"]):
            excluded_st+=1;continue
        if _is_limit_up(last,prev):
            excluded_limit_up+=1;continue
        local_params=dict(params)
        code=str(last.code)
        local_params["_b1_amplitude_limit"]=7.0 if (
            str(last.exchange)=="BJ" or code.startswith(("300","301","688"))
        ) else 4.0
        try:signal=fn(bars[["date","open","close","high","low","volume","amount"]].reset_index(drop=True),local_params)
        except Exception:continue
        if not signal:continue
        prev=prev if prev is not None else last;score=float(signal.get("score",0));details=signal.get("details",{})
        results.append({"instrument_id":instrument_id,"market":{"SZ":0,"SH":1,"BJ":2}[last.exchange],"exchange":last.exchange,"code":last.code,"name":last["name"],"date":last.date.date().isoformat() if hasattr(last.date,"date") else str(last.date),"close":round(float(last.close),4),"change":round((float(last.close)/float(prev.close)-1)*100 if prev.close else 0,4),"score":round(score,4),"signals":signal.get("signals",[]),"details":details})
    results.sort(key=lambda x:x["score"],reverse=True);resolved=max(x["date"] for x in results) if results else str(df.date.max().date())
    payload={"strategy":meta,"requested_date":target.isoformat(),"resolved_date":resolved,"universe":universe,"scanned":int(df.instrument_id.nunique()),"matched":len(results),"results":results[:limit],"filters":{"exclude_st":True,"exclude_limit_up":True,"excluded_st":excluded_st,"excluded_limit_up":excluded_limit_up}}
    if "_active_market_gate" in params: payload["active_market_gate"]={k:v for k,v in params["_active_market_gate"].items() if not k.startswith("_")}
    return payload
