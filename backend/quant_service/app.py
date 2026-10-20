from __future__ import annotations
from dataclasses import asdict, is_dataclass
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from .engine import load_kline_rows, run_backtest
from .strategy_loader import import_strategy, list_strategies, strategy_source_bundle
from .screener_engine import run_screen
from .screener_loader import import_screener, import_tdx_screener, list_screeners

DEFAULT_ALLOWED_ORIGINS = ["http://localhost:3100", "http://127.0.0.1:3100"]
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "QUANTWEAVE_ALLOWED_ORIGINS",
        ",".join(DEFAULT_ALLOWED_ORIGINS),
    ).split(",")
    if origin.strip()
]

app=FastAPI(title="量构 QuantWeave 本地回测 API",version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)
RESULTS={}

class BacktestRequest(BaseModel):
    strategy_id:str="ma_cross";code:str=Field(pattern=r"^\d{6}$");name:str|None=None;start_date:str|None=None;end_date:str|None=None;capital:float=1_000_000;commission:float=.0003;slippage:float=.0001;exclude_limit_up:bool=True;params:dict=Field(default_factory=dict)
class ImportRequest(BaseModel):strategy_id:str;code:str
class TdxImportRequest(BaseModel):strategy_id:str;name:str;formula:str;lookback:int=250
class ScreenRequest(BaseModel):strategy_id:str;date:str;universe:str="沪深京";limit:int=200;params:dict=Field(default_factory=dict)
class LegoPipelineRequest(BaseModel):pipeline:dict[str,Any]
class LegoSaveRequest(BaseModel):pipeline:dict[str,Any];name:str
class LegoCustomBlockRequest(BaseModel):block:dict[str,Any];template_id:str|None=None;description:str=""
class LegoFormulaRequest(BaseModel):formula:str;block_type:str
class FactorLabRequest(BaseModel):config:dict[str,Any]=Field(default_factory=dict)
class AiMessage(BaseModel):
    role:str=Field(pattern=r"^(user|assistant)$")
    content:str=Field(min_length=1,max_length=12000)
class AiChatRequest(BaseModel):
    messages:list[AiMessage]=Field(min_length=1,max_length=20)
    tools:list[str]=Field(default_factory=list,max_length=20)
    context:dict[str,Any]|None=None
class FactorTeachingRequest(BaseModel):
    factor_type:str
    code:str=Field(default="600519",pattern=r"^\d{6}$")
    params:dict[str,Any]=Field(default_factory=dict)
    limit:int=Field(default=120,ge=60,le=240)


def _records(value: Any, limit: int | None = None) -> list[dict[str, Any]]:
    if not isinstance(value, pd.DataFrame) or value.empty:
        return []
    frame = value.head(limit) if limit else value
    return json.loads(frame.to_json(orient="records", date_format="iso"))


def _enrich_closed_trades(closed_value: Any, trades_value: Any = None) -> pd.DataFrame:
    """Backfill cost/price columns for runs saved before those fields existed.

    A closed row is emitted at the same time as its sell fill.  Matching by
    instrument, exit date and occurrence number therefore reconstructs the
    historical values without guessing which buy lots were consumed.  When an
    old run has no detailed sell rows, ``pnl / realized_return`` remains an
    exact fallback for the all-in entry cost whenever the return is non-zero.
    """
    if not isinstance(closed_value, pd.DataFrame) or closed_value.empty:
        return pd.DataFrame()
    frame = closed_value.copy().reset_index(drop=True)
    for column in ("pnl", "gross_pnl", "realized_return", "shares"):
        if column not in frame:
            frame[column] = 0.0
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in ("entry_cost", "exit_proceeds", "entry_price", "exit_price"):
        if column not in frame:
            frame[column] = float("nan")
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    sells = pd.DataFrame()
    if isinstance(trades_value, pd.DataFrame) and not trades_value.empty and "side" in trades_value:
        sells = trades_value[trades_value["side"].astype(str) == "卖出"].copy()
    required = {"code", "date", "price", "shares", "amount", "fee"}
    if not sells.empty and required.issubset(sells.columns) and {"code", "exit_date"}.issubset(frame.columns):
        sells["_exit_key"] = pd.to_datetime(sells["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        frame["_exit_key"] = pd.to_datetime(frame["exit_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        sells["_match_no"] = sells.groupby(["code", "_exit_key"], dropna=False).cumcount()
        frame["_match_no"] = frame.groupby(["code", "_exit_key"], dropna=False).cumcount()
        sell_columns = sells[["code", "_exit_key", "_match_no", "price", "amount", "fee"]].rename(
            columns={"price": "_sell_price", "amount": "_sell_amount", "fee": "_sell_fee"}
        )
        frame = frame.merge(sell_columns, on=["code", "_exit_key", "_match_no"], how="left", validate="one_to_one")
        sell_amount = pd.to_numeric(frame["_sell_amount"], errors="coerce")
        sell_fee = pd.to_numeric(frame["_sell_fee"], errors="coerce").fillna(0.0)
        shares = frame["shares"].replace(0, float("nan"))
        net_proceeds = sell_amount - sell_fee
        frame["exit_proceeds"] = frame["exit_proceeds"].fillna(net_proceeds)
        frame["entry_cost"] = frame["entry_cost"].fillna(net_proceeds - frame["pnl"])
        frame["exit_price"] = frame["exit_price"].fillna(pd.to_numeric(frame["_sell_price"], errors="coerce"))
        frame["entry_price"] = frame["entry_price"].fillna((sell_amount - frame["gross_pnl"]) / shares)
        frame = frame.drop(columns=[
            "_exit_key", "_match_no", "_sell_price", "_sell_amount", "_sell_fee",
        ])

    nonzero_return = frame["realized_return"].abs() > 1e-15
    inferred_cost = (frame["pnl"] / frame["realized_return"]).where(nonzero_return)
    frame["entry_cost"] = frame["entry_cost"].fillna(inferred_cost)
    frame["exit_proceeds"] = frame["exit_proceeds"].fillna(frame["entry_cost"] + frame["pnl"])
    shares = frame["shares"].replace(0, float("nan"))
    frame["entry_price"] = frame["entry_price"].fillna(frame["entry_cost"] / shares)
    frame["exit_price"] = frame["exit_price"].fillna(frame["exit_proceeds"] / shares)
    return frame


def _stock_pnl_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, pd.DataFrame) or value.empty:
        return []
    frame = value.copy()
    for column in ("pnl", "gross_pnl", "realized_return", "entry_cost", "exit_proceeds"):
        if column not in frame:
            frame[column] = 0.0
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
    frame["wins"] = (frame["pnl"] > 0).astype(int)
    frame["losses"] = (frame["pnl"] < 0).astype(int)
    frame["breakeven"] = (frame["pnl"] == 0).astype(int)
    grouped = frame.groupby(["code", "ticker", "name"], dropna=False).agg(
        trades=("pnl", "size"), wins=("wins", "sum"), losses=("losses", "sum"),
        breakeven=("breakeven", "sum"), pnl=("pnl", "sum"),
        gross_pnl=("gross_pnl", "sum"), entry_cost=("entry_cost", "sum"),
        exit_proceeds=("exit_proceeds", "sum"),
        average_return=("realized_return", "mean"),
    ).reset_index()
    grouped["realized_return"] = grouped.apply(
        lambda row: row["pnl"] / row["entry_cost"] if row["entry_cost"] else row["average_return"], axis=1
    )
    grouped = grouped.sort_values(["pnl", "trades"], ascending=[False, False])
    return json.loads(grouped.to_json(orient="records"))


def _lego_payload(result: dict[str, Any]) -> dict[str, Any]:
    strategy = result.get("strategy", {})
    closed_trades = _enrich_closed_trades(result.get("closed_trades"), result.get("trades"))
    rank_details = _records(result.get("rank_details"))
    return {
        "run_id": result.get("run_id"),
        "created_at": result.get("created_at"),
        "config": result.get("config", {}),
        "strategy": asdict(strategy) if is_dataclass(strategy) else dict(strategy or {}),
        "metrics": result.get("metrics", {}),
        "coverage": result.get("coverage", {}),
        "equity": _records(result.get("equity")),
        "trades": _records(result.get("trades")),
        "closed_trades": _records(closed_trades),
        "stock_pnl": _stock_pnl_records(closed_trades),
        "stage_funnel": _records(result.get("stage_funnel")),
        "rank_details": rank_details,
        "rank_preview": rank_details,
        "factor_analysis": _records(result.get("factor_analysis")),
        "factor_groups": _records(result.get("factor_groups")),
        "factor_correlations": _records(result.get("factor_correlations")),
    }


def _factor_lab_payload(result: dict[str, Any]) -> dict[str, Any]:
    model_result = result.get("model_result")
    baseline = _lego_payload(result.get("baseline", {}))
    baseline["rank_details"] = []
    baseline["rank_preview"] = []
    return {
        "pipeline": result.get("pipeline", {}),
        "selected_factors": result.get("selected_factors", []),
        "baseline": baseline,
        "model_summary": result.get("model_summary"),
        "model_result": _lego_payload(model_result) if isinstance(model_result, dict) else None,
        "notes": result.get("notes", {}),
    }

@app.get("/health")
def health():return {"ok":True,"engine":"Backtrader","service":"QuantWeave local backtest API"}
@app.get("/api/v1/ai/status")
def ai_status():
    from .ai_gateway import AiGatewayError, status_payload

    try:return status_payload()
    except AiGatewayError as exc:raise HTTPException(400,str(exc))
@app.post("/api/v1/ai/chat")
def ai_chat(req:AiChatRequest):
    from .ai_gateway import AiGatewayError, chat

    try:return chat([item.model_dump() for item in req.messages],req.tools,req.context)
    except AiGatewayError as exc:raise HTTPException(503,str(exc))
@app.get("/api/v1/data/scheduler")
def data_scheduler_status():
    """Read scheduler state only. Starting a data update stays a local CLI action."""
    root = Path(__file__).resolve().parents[1]
    state_path = root / "work" / "data-scheduler" / "status.json"
    try:
        scheduler = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(scheduler, dict):
            scheduler = {}
    except (OSError, json.JSONDecodeError):
        scheduler = {}
    try:
        from backtest_lab.data import database_summary

        database = database_summary()
    except Exception as exc:
        database = {"error": str(exc)}
    return {
        "configured": True,
        "running": scheduler.get("status") == "running",
        "scheduler": scheduler or {"status": "not-started"},
        "database": database,
        "commands": {
            "once": "npm run data:update",
            "scheduler": "npm run data:scheduler",
            "dry_run": "npm run data:update:dry-run",
        },
    }
@app.get("/api/v1/workspace/check")
def workspace_check():
    """Check and repair local web catalogs without downloading market data."""
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "check_ruipan_workspace.py"
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        output = (result.stdout or result.stderr).strip().splitlines()
        payload = json.loads(output[-1]) if output else {"status": "warning", "error": "自检没有返回结果"}
        if result.returncode != 0:
            payload["status"] = "warning"
            payload["error"] = output[-1] if output else "自检进程失败"
        return payload
    except Exception as exc:
        raise HTTPException(503, f"本地自检失败: {exc}")
@app.get("/api/v1/lego/catalog")
def lego_catalog():
    from backtest_lab.lego_pipeline import (
        blank_pipeline,
        catalog_with_custom_templates,
        list_pipelines,
        standard_multifactor_pipeline,
    )

    pipeline=asdict(blank_pipeline())
    return {
        "catalog":catalog_with_custom_templates(),
        "default_pipeline":pipeline,
        "blank_pipeline":pipeline,
        "presets":{
            "standard_multifactor":asdict(standard_multifactor_pipeline()),
        },
        "saved":list_pipelines(),
    }
@app.post("/api/v1/lego/validate-formula")
def lego_formula_validate(req:LegoFormulaRequest):
    from backtest_lab.lego_pipeline import validate_custom_formula

    try:return validate_custom_formula(req.formula,req.block_type)
    except Exception as exc:raise HTTPException(400,str(exc))
@app.post("/api/v1/lego/custom-blocks")
def lego_custom_block_save(req:LegoCustomBlockRequest):
    from backtest_lab.lego_pipeline import catalog_with_custom_templates, save_custom_block_template

    try:
        result=save_custom_block_template(req.block,req.template_id,req.description)
        result["catalog"]=catalog_with_custom_templates()
        return result
    except Exception as exc:raise HTTPException(400,str(exc))
@app.post("/api/v1/lego/factor-example")
def lego_factor_example(req:FactorTeachingRequest):
    from backtest_lab.lego_pipeline import factor_teaching_example

    try:return factor_teaching_example(req.factor_type,req.code,req.params,req.limit)
    except FileNotFoundError as exc:raise HTTPException(404,str(exc))
    except Exception as exc:raise HTTPException(400,str(exc))
@app.post("/api/v1/lego/validate")
def lego_validate(req:LegoPipelineRequest):
    from backtest_lab.lego_pipeline import validate_pipeline

    try:return {"ok":True,"pipeline":asdict(validate_pipeline(req.pipeline))}
    except Exception as exc:raise HTTPException(400,str(exc))
@app.post("/api/v1/lego/backtests")
def lego_backtest(req:LegoPipelineRequest):
    from backtest_lab.lego_pipeline import run_lego_backtest

    try:return _lego_payload(run_lego_backtest(req.pipeline,persist=True))
    except Exception as exc:raise HTTPException(400,str(exc))


@app.get("/api/v1/factor-lab/catalog")
def factor_lab_catalog_get():
    from backtest_lab.factor_lab import factor_lab_catalog

    return factor_lab_catalog()


@app.post("/api/v1/factor-lab/run")
def factor_lab_run(req:FactorLabRequest):
    from backtest_lab.factor_lab import run_factor_lab

    try:return _factor_lab_payload(run_factor_lab(req.config))
    except Exception as exc:raise HTTPException(400,str(exc))
@app.get("/api/v1/lego/pipelines")
def lego_pipelines():
    from backtest_lab.lego_pipeline import list_pipelines

    return {"pipelines":list_pipelines()}
@app.get("/api/v1/lego/pipelines/{pipeline_id}")
def lego_pipeline_get(pipeline_id:str):
    from backtest_lab.lego_pipeline import load_pipeline

    try:return {"pipeline":asdict(load_pipeline(pipeline_id))}
    except FileNotFoundError as exc:raise HTTPException(404,str(exc))
    except Exception as exc:raise HTTPException(400,str(exc))
@app.post("/api/v1/lego/pipelines")
def lego_pipeline_save(req:LegoSaveRequest):
    from backtest_lab.lego_pipeline import save_pipeline

    try:return save_pipeline(req.pipeline,req.name)
    except Exception as exc:raise HTTPException(400,str(exc))
@app.get("/api/v1/lego/pipelines/{pipeline_id}/versions")
def lego_pipeline_versions(pipeline_id:str):
    from backtest_lab.lego_pipeline import list_pipeline_versions

    try:return {"versions":list_pipeline_versions(pipeline_id)}
    except Exception as exc:raise HTTPException(400,str(exc))
@app.get("/api/v1/lego/pipelines/{pipeline_id}/versions/{revision}")
def lego_pipeline_version_get(pipeline_id:str,revision:int):
    from backtest_lab.lego_pipeline import load_pipeline_version

    try:return {"pipeline":asdict(load_pipeline_version(pipeline_id,revision)),"revision":revision}
    except FileNotFoundError as exc:raise HTTPException(404,str(exc))
    except Exception as exc:raise HTTPException(400,str(exc))
@app.post("/api/v1/lego/pipelines/{pipeline_id}/versions/{revision}/restore")
def lego_pipeline_version_restore(pipeline_id:str,revision:int):
    from backtest_lab.lego_pipeline import restore_pipeline_version

    try:return restore_pipeline_version(pipeline_id,revision)
    except FileNotFoundError as exc:raise HTTPException(404,str(exc))
    except Exception as exc:raise HTTPException(400,str(exc))
@app.delete("/api/v1/lego/pipelines/{pipeline_id}")
def lego_pipeline_delete(pipeline_id:str):
    from backtest_lab.lego_pipeline import delete_pipeline

    try:return delete_pipeline(pipeline_id)
    except FileNotFoundError as exc:raise HTTPException(404,str(exc))
    except Exception as exc:raise HTTPException(400,str(exc))
@app.get("/api/v1/lego/runs")
def lego_runs(pipeline_id:str="",limit:int=30):
    from backtest_lab.engine import list_runs

    rows=[]
    for item in list_runs(max(1,min(limit,100))):
        config=dict(item.get("config") or {})
        if pipeline_id and config.get("pipeline_id")!=pipeline_id:
            continue
        rows.append({
            "run_id":item.get("run_id"),"created_at":item.get("created_at"),
            "pipeline_id":config.get("pipeline_id"),
            "pipeline_name":config.get("pipeline_name") or item.get("pipeline_name") or item.get("strategy",{}).get("name"),
            "start":config.get("start"),"end":config.get("end"),
            "metrics":item.get("metrics",{}),"coverage":item.get("coverage",{}),
        })
    return {"runs":rows}
@app.get("/api/v1/lego/runs/{run_id}")
def lego_run_get(run_id:str):
    from backtest_lab.engine import load_run

    try:return _lego_payload(load_run(run_id))
    except FileNotFoundError as exc:raise HTTPException(404,str(exc))
    except Exception as exc:raise HTTPException(400,str(exc))
@app.get("/api/v1/strategies")
def strategies():return {"strategies":list_strategies()}
@app.get("/api/v1/strategies/{strategy_id}/source")
def strategy_source(strategy_id:str):
    try:return strategy_source_bundle(strategy_id)
    except FileNotFoundError as exc:raise HTTPException(404,str(exc))
    except Exception as exc:raise HTTPException(400,str(exc))
@app.get("/api/v1/screeners")
def screeners():return {"screeners":list_screeners()}
@app.post("/api/v1/screeners/import")
def screener_import(req:ImportRequest):
    try:return import_screener(req.strategy_id,req.code)
    except Exception as exc:raise HTTPException(400,str(exc))
@app.post("/api/v1/screeners/import-tdx")
def screener_import_tdx(req:TdxImportRequest):
    try:return import_tdx_screener(req.strategy_id,req.name,req.formula,req.lookback)
    except Exception as exc:raise HTTPException(400,str(exc))
@app.post("/api/v1/screens")
def screens(req:ScreenRequest):
    try:return run_screen(req.model_dump())
    except Exception as exc:raise HTTPException(400,str(exc))
@app.get("/api/v1/klines")
def klines(market:int,code:str,period:str="日线",adjustment:str="不复权"):
    if len(code)!=6 or not code.isdigit():raise HTTPException(400,"证券代码必须是6位数字")
    try:
        candles=load_kline_rows(market,code,period,adjustment)
        if not candles:raise HTTPException(404,"本地数据库暂无该证券行情")
        return {"source":"local-duckdb","period":period,"adjustment":adjustment,"candles":candles}
    except HTTPException:raise
    except Exception as exc:raise HTTPException(400,str(exc))
@app.post("/api/v1/strategies/import")
def strategy_import(req:ImportRequest):
    try:return import_strategy(req.strategy_id,req.code)
    except Exception as exc:raise HTTPException(400,str(exc))
@app.post("/api/v1/backtests")
def backtests(req:BacktestRequest):
    try:
        result=run_backtest(req.model_dump());run_id=uuid.uuid4().hex[:12];result["run_id"]=run_id;RESULTS[run_id]=result;return result
    except Exception as exc:raise HTTPException(400,str(exc))
@app.get("/api/v1/backtests/{run_id}")
def result(run_id:str):
    if run_id not in RESULTS:raise HTTPException(404,"回测记录不存在或服务已重启")
    return RESULTS[run_id]
