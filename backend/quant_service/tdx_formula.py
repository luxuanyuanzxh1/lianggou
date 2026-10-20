from __future__ import annotations
import re
import numpy as np, pandas as pd

def _s(x,index):return x if isinstance(x,pd.Series) else pd.Series(x,index=index,dtype=float)
def evaluate_formula(bars:pd.DataFrame,formula:str):
    index=bars.index
    def MA(x,n):return _s(x,index).rolling(int(n)).mean()
    def EMA(x,n):return _s(x,index).ewm(span=int(n),adjust=False).mean()
    def SMA(x,n,m=1):return _s(x,index).ewm(alpha=float(m)/float(n),adjust=False).mean()
    def REF(x,n):return _s(x,index).shift(int(n))
    def HHV(x,n):return _s(x,index).rolling(int(n)).max()
    def LLV(x,n):return _s(x,index).rolling(int(n)).min()
    def SUM(x,n):return _s(x,index).rolling(int(n)).sum()
    def COUNT(x,n):return _s(x,index).astype(bool).rolling(int(n)).sum()
    def EVERY(x,n):return COUNT(x,n)==int(n)
    def EXIST(x,n):return COUNT(x,n)>0
    def CROSS(a,b):a,b=_s(a,index),_s(b,index);return (a>b)&(a.shift(1)<=b.shift(1))
    def IF(c,a,b):return pd.Series(np.where(_s(c,index),_s(a,index),_s(b,index)),index=index)
    def MAX(a,b):return pd.concat([_s(a,index),_s(b,index)],axis=1).max(axis=1)
    def MIN(a,b):return pd.concat([_s(a,index),_s(b,index)],axis=1).min(axis=1)
    def STD(x,n):return _s(x,index).rolling(int(n)).std(ddof=0)
    def BETWEEN(x,a,b):x,a,b=_s(x,index),_s(a,index),_s(b,index);return (x>=a)&(x<=b)
    env={"C":bars.close,"CLOSE":bars.close,"O":bars.open,"OPEN":bars.open,"H":bars.high,"HIGH":bars.high,"L":bars.low,"LOW":bars.low,"V":bars.volume,"VOL":bars.volume,"VOLUME":bars.volume,"AMOUNT":bars.amount,"MA":MA,"EMA":EMA,"SMA":SMA,"REF":REF,"HHV":HHV,"LLV":LLV,"SUM":SUM,"COUNT":COUNT,"EVERY":EVERY,"EXIST":EXIST,"CROSS":CROSS,"IF":IF,"MAX":MAX,"MIN":MIN,"ABS":lambda x:_s(x,index).abs(),"STD":STD,"BETWEEN":BETWEEN}
    clean=re.sub(r"\{[^}]*\}","",formula.replace("；",";").replace("：",":"))
    output=None;details={}
    for raw in clean.split(";"):
        statement=raw.strip()
        if not statement:continue
        if ":=" in statement:name,expr=statement.split(":=",1);is_output=False
        elif ":" in statement:name,expr=statement.split(":",1);is_output=True
        else:name,expr="XG",statement;is_output=True
        name=name.strip().upper();expr=expr.strip().upper().replace("&&"," AND ").replace("||"," OR ").replace("<>","!=")
        expr=re.sub(r"(?<![<>=!:])=(?!=)","==",expr);expr=re.sub(r"\bNOT\b","~",expr);expr="("+re.sub(r"\bAND\b",") & (",re.sub(r"\bOR\b",") | (",expr))+")"
        try:value=eval(expr,{"__builtins__":{}},env)
        except Exception as exc:raise ValueError(f"通达信公式解析失败 [{name}]: {exc}") from exc
        env[name]=value
        if isinstance(value,pd.Series) and len(value):
            last=value.iloc[-1];details[name]=round(float(last),4) if pd.notna(last) and not isinstance(last,(bool,np.bool_)) else bool(last) if pd.notna(last) else None
        if is_output:output=value
    if output is None:raise ValueError("通达信公式缺少选股输出，例如 XG:条件;")
    matched=bool(_s(output,index).fillna(False).iloc[-1]);return {"matched":matched,"details":details}
