"""Dedicated factor research and walk-forward model experiments for QuantWeave.

The factor lab intentionally reuses the audited Lego factor engine and the
full-market A-share execution simulator.  Machine-learning labels are based on
future executable opens, carry an explicit maturity date, and are only admitted
to a training window after that maturity date has passed.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
import importlib.util
import math
from typing import Any

import duckdb
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge

from .config import MARKET_DB
from .engine import BacktestConfig
from .full_market import run_full_market_backtest
from .lego_pipeline import (
    BLOCK_CATALOG,
    BlockSpec,
    PipelineSpec,
    standard_multifactor_pipeline,
    run_lego_backtest,
    validate_custom_formula,
)


FACTOR_CATALOG: dict[str, dict[str, Any]] = {
    "momentum_20": {
        "name": "20日动量",
        "description": "当前收盘相对20个交易日前收盘的收益。",
        "family": "动量",
        "type": "factor_momentum",
        "params": {"lookback": 20, "direction": "higher", "method": "percentile"},
    },
    "momentum_60": {
        "name": "60日动量",
        "description": "中期价格动量，适合观察趋势持续性。",
        "family": "动量",
        "type": "factor_momentum",
        "params": {"lookback": 60, "direction": "higher", "method": "percentile"},
    },
    "reversal_5": {
        "name": "5日反转",
        "description": "偏好过去5日相对弱势股票，检查短期均值回归。",
        "family": "反转",
        "type": "factor_custom_formula",
        "params": {"name": "5日反转", "formula": "MOM(5)", "direction": "lower", "method": "percentile"},
    },
    "trend_10_60": {
        "name": "10/60日趋势",
        "description": "10日均线相对60日均线的趋势强度。",
        "family": "趋势",
        "type": "factor_trend",
        "params": {"fast": 10, "slow": 60, "direction": "higher", "method": "percentile"},
    },
    "price_to_ma20": {
        "name": "价格偏离MA20",
        "description": "收盘价相对20日均线的偏离程度。",
        "family": "趋势",
        "type": "factor_custom_formula",
        "params": {"name": "价格偏离MA20", "formula": "close / MA(20) - 1", "direction": "higher", "method": "robust_sigmoid"},
    },
    "rsi_14": {
        "name": "RSI14强度",
        "description": "14日RSI横截面强弱。",
        "family": "摆动",
        "type": "factor_rsi",
        "params": {"period": 14, "direction": "higher", "method": "percentile"},
    },
    "low_vol_20": {
        "name": "20日低波动",
        "description": "偏好过去20日收益波动率较低的股票。",
        "family": "风险",
        "type": "factor_low_volatility",
        "params": {"window": 20, "direction": "lower", "method": "percentile"},
    },
    "drawdown_60": {
        "name": "60日回撤",
        "description": "收盘价相对60日最高收盘的回撤，越接近零越好。",
        "family": "风险",
        "type": "factor_drawdown",
        "params": {"window": 60, "direction": "higher", "method": "percentile"},
    },
    "liquidity_20": {
        "name": "20日流动性",
        "description": "过去20日平均成交额，偏好更高流动性。",
        "family": "流动性",
        "type": "factor_liquidity",
        "params": {"window": 20, "direction": "higher", "method": "robust_sigmoid"},
    },
    "small_liquidity_20": {
        "name": "流动性下沿",
        "description": "在成交额硬门槛之上偏好相对较小的20日平均成交额。",
        "family": "流动性",
        "type": "factor_liquidity",
        "params": {"window": 20, "direction": "lower", "method": "percentile"},
    },
    "intraday_strength": {
        "name": "日内收盘位置",
        "description": "收盘价位于当日高低区间的位置。",
        "family": "量价",
        "type": "factor_custom_formula",
        "params": {"name": "日内收盘位置", "formula": "(close - low) / (high - low + 0.000001)", "direction": "higher", "method": "percentile"},
    },
    "amplitude": {
        "name": "日内振幅",
        "description": "当日高低价差相对昨收的比例。",
        "family": "风险",
        "type": "factor_custom_formula",
        "params": {"name": "日内振幅", "formula": "(high - low) / prev_close", "direction": "lower", "method": "robust_sigmoid"},
    },
}

MODEL_CATALOG = {
    "ridge": {"name": "Ridge线性基线", "available": True, "kind": "linear"},
    "hist_gb": {"name": "直方图梯度提升", "available": True, "kind": "tree"},
    "lightgbm": {
        "name": "LightGBM",
        "available": importlib.util.find_spec("lightgbm") is not None,
        "kind": "tree",
    },
    "xgboost": {
        "name": "XGBoost",
        "available": importlib.util.find_spec("xgboost") is not None,
        "kind": "tree",
        "disabled_reason": "当前Python环境尚未安装xgboost" if importlib.util.find_spec("xgboost") is None else "",
    },
}


def factor_lab_catalog() -> dict[str, Any]:
    preset = standard_multifactor_pipeline()
    return {
        "factors": [{"id": key, **value} for key, value in FACTOR_CATALOG.items()],
        "models": [{"id": key, **value} for key, value in MODEL_CATALOG.items()],
        "defaults": {
            "start_date": preset.start,
            "end_date": preset.end,
            "universe": "all",
            "positions": 8,
            "rebalance_every": 10,
            "research_pool": 300,
            "label_horizon": 5,
            "train_window": 252,
            "retrain_every": 20,
            "model": "lightgbm" if MODEL_CATALOG["lightgbm"]["available"] else "hist_gb",
            "factors": ["momentum_60", "trend_10_60", "low_vol_20", "liquidity_20"],
        },
        "safety": {
            "signal_execution": "T日收盘冻结，T+1开盘执行",
            "label_policy": "只有maturity_date不晚于当前训练日的标签可进入训练",
            "validation": "滚动时间窗；禁止随机打乱和全样本回填",
            "position_caps": "单票不超过10%，总仓不超过80%",
        },
    }


def _selected_factors(request: dict[str, Any]) -> list[dict[str, Any]]:
    raw = request.get("factors") or factor_lab_catalog()["defaults"]["factors"]
    if not isinstance(raw, list) or not raw:
        raise ValueError("至少选择一个因子")
    custom_raw = request.get("custom_factors") or []
    if not isinstance(custom_raw, list):
        raise ValueError("自定义因子格式无效")
    if len(raw) + len(custom_raw) > 12:
        raise ValueError("单次实验最多选择12个因子")
    selected: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        payload = {"id": item} if isinstance(item, str) else dict(item)
        factor_id = str(payload.get("id", ""))
        if factor_id not in FACTOR_CATALOG:
            raise ValueError(f"不支持的因子：{factor_id}")
        definition = FACTOR_CATALOG[factor_id]
        weight = float(payload.get("weight", 1.0))
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError(f"因子权重必须大于0：{definition['name']}")
        params = {**definition["params"], **dict(payload.get("params") or {})}
        selected.append({
            "id": factor_id,
            "block_id": f"lab-factor-{index + 1}",
            "name": definition["name"],
            "type": definition["type"],
            "weight": weight,
            "params": params,
        })
    for custom_index, raw_custom in enumerate(custom_raw):
        custom = dict(raw_custom or {})
        name = str(custom.get("name") or f"自定义因子{custom_index + 1}").strip()[:40]
        formula = str(custom.get("formula") or "").strip()
        validate_custom_formula(formula, "factor_custom_formula")
        direction = str(custom.get("direction", "higher"))
        if direction not in {"higher", "lower"}:
            raise ValueError(f"自定义因子方向无效：{name}")
        weight = float(custom.get("weight", 1.0))
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError(f"自定义因子权重必须大于0：{name}")
        selected.append({
            "id": f"custom_{custom_index + 1}",
            "block_id": f"lab-factor-{len(selected) + 1}",
            "name": name,
            "type": "factor_custom_formula",
            "weight": weight,
            "params": {
                "name": name, "formula": formula, "direction": direction,
                "method": str(custom.get("method", "robust_sigmoid")),
            },
        })
    total = sum(item["weight"] for item in selected)
    for item in selected:
        item["weight"] /= total
    return selected


def _build_pipeline(request: dict[str, Any], selected: list[dict[str, Any]]) -> PipelineSpec:
    preset = standard_multifactor_pipeline()
    universe = str(request.get("universe", "all"))
    if universe not in {"all", "sh_sz", "main", "star", "chinext", "bj"}:
        raise ValueError("因子实验股票池不受支持")
    start = str(request.get("start_date") or preset.start)
    end = str(request.get("end_date") or preset.end)
    if pd.Timestamp(start) >= pd.Timestamp(end):
        raise ValueError("开始日期必须早于结束日期")
    positions = max(1, min(30, int(request.get("positions", 8))))
    rebalance = max(1, min(60, int(request.get("rebalance_every", 10))))
    research_pool = max(positions, min(1000, int(request.get("research_pool", 300))))
    min_amount = max(0.0, float(request.get("min_amount", 30_000_000.0)))

    blocks: list[BlockSpec] = []
    for block in preset.blocks:
        if block.category in {"factor", "penalty"} or block.type == "strategy_momentum":
            continue
        if block.category == "universe":
            blocks.append(BlockSpec(block.id, "universe", params={"scope": universe, "codes": []}))
        elif block.type == "filter_adv20":
            blocks.append(BlockSpec(block.id, block.type, params={"window": 20, "min_amount": min_amount}))
        elif block.category == "rank":
            blocks.append(BlockSpec(block.id, block.type, params={"top_k": research_pool}))
        elif block.category == "portfolio":
            blocks.append(BlockSpec(block.id, block.type, params={
                "positions": positions, "rebalance_every": rebalance, "weighting": "equal",
            }))
        else:
            blocks.append(block)

    normalizer_index = next(i for i, block in enumerate(blocks) if block.category == "normalize")
    factor_blocks = [
        BlockSpec(
            item["block_id"], item["type"],
            name=item["name"],
            params={**item["params"], "weight": item["weight"]},
        )
        for item in selected
    ]
    blocks[normalizer_index:normalizer_index] = factor_blocks
    return PipelineSpec(
        id="factor-lab-experiment",
        name=str(request.get("name") or "因子实验室研究"),
        description="独立因子实验室；因子诊断、滚动机器学习与A股次日开盘回测。",
        version=1,
        scope=universe,
        start=pd.Timestamp(start).date().isoformat(),
        end=pd.Timestamp(end).date().isoformat(),
        initial_cash=max(100_000.0, float(request.get("initial_cash", 1_000_000.0))),
        blocks=blocks,
        fees={},
        workflow={"source": "factor-lab"},
    )


def _attach_execution_labels(
    details: pd.DataFrame,
    horizon: int,
    end_date: str,
) -> pd.DataFrame:
    if details.empty:
        return details
    horizon = max(1, min(60, int(horizon)))
    keys = details[["instrument_id", "signal_date"]].drop_duplicates().copy()
    keys["signal_date"] = pd.to_datetime(keys["signal_date"]).dt.date
    with duckdb.connect(str(MARKET_DB), read_only=True) as connection:
        connection.register("factor_lab_keys", keys)
        labels = connection.execute(
            f"""
            WITH instruments AS (
                SELECT DISTINCT instrument_id FROM factor_lab_keys
            ), bars AS (
                SELECT d.instrument_id,d.date,
                       lead(d.open,1) OVER(PARTITION BY d.instrument_id ORDER BY d.date) AS entry_open,
                       lead(d.open,{horizon + 1}) OVER(PARTITION BY d.instrument_id ORDER BY d.date) AS exit_open,
                       lead(d.date,{horizon + 1}) OVER(PARTITION BY d.instrument_id ORDER BY d.date) AS maturity_date
                FROM daily_bar d JOIN instruments USING(instrument_id)
                WHERE d.date <= ?
            )
            SELECT k.instrument_id,k.signal_date,b.maturity_date,
                   b.exit_open/nullif(b.entry_open,0)-1-0.00134 AS forward_return
            FROM factor_lab_keys k JOIN bars b
              ON b.instrument_id=k.instrument_id AND b.date=k.signal_date
            """,
            [pd.Timestamp(end_date).date()],
        ).df()
        connection.unregister("factor_lab_keys")
    labels["signal_date"] = pd.to_datetime(labels["signal_date"])
    labels["maturity_date"] = pd.to_datetime(labels["maturity_date"])
    frame = details.copy()
    frame["signal_date"] = pd.to_datetime(frame["signal_date"])
    return frame.merge(labels, on=["instrument_id", "signal_date"], how="left")


def _make_model(model_id: str, random_state: int):
    if model_id == "ridge":
        return Ridge(alpha=5.0, random_state=random_state)
    if model_id == "hist_gb":
        return HistGradientBoostingRegressor(
            max_iter=60, max_leaf_nodes=15, learning_rate=0.05,
            l2_regularization=5.0, random_state=random_state,
        )
    if model_id == "lightgbm":
        from lightgbm import LGBMRegressor

        return LGBMRegressor(
            n_estimators=100, learning_rate=0.04, num_leaves=15,
            max_depth=5, subsample=0.85, colsample_bytree=0.85,
            reg_lambda=5.0, random_state=random_state, verbosity=-1,
        )
    if model_id == "xgboost":
        from xgboost import XGBRegressor

        return XGBRegressor(
            objective="reg:squarederror", n_estimators=120,
            learning_rate=0.04, max_depth=4, min_child_weight=10,
            subsample=0.85, colsample_bytree=0.85,
            reg_lambda=8.0, random_state=random_state, n_jobs=4,
        )
    raise ValueError(f"不支持的模型：{model_id}")


def _feature_importance(model: Any, feature_columns: list[str]) -> list[dict[str, Any]]:
    values: np.ndarray
    if hasattr(model, "feature_importances_"):
        values = np.asarray(model.feature_importances_, dtype=float)
    elif hasattr(model, "coef_"):
        values = np.abs(np.asarray(model.coef_, dtype=float).reshape(-1))
    else:
        values = np.zeros(len(feature_columns), dtype=float)
    total = float(values.sum())
    if total > 0:
        values /= total
    rows = [
        {"feature": column, "importance": float(value)}
        for column, value in zip(feature_columns, values, strict=False)
    ]
    return sorted(rows, key=lambda row: row["importance"], reverse=True)


def _walk_forward_model(
    dataset: pd.DataFrame,
    feature_columns: list[str],
    request: dict[str, Any],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    model_id = str(request.get("model", "hist_gb"))
    model_meta = MODEL_CATALOG.get(model_id)
    if not model_meta:
        raise ValueError("未知机器学习模型")
    if not model_meta["available"]:
        raise ValueError(str(model_meta.get("disabled_reason") or f"{model_meta['name']}当前不可用"))
    train_window = max(60, min(1000, int(request.get("train_window", 252))))
    retrain_every = max(1, min(120, int(request.get("retrain_every", 20))))
    min_train_dates = max(20, min(train_window, int(request.get("min_train_dates", 80))))
    random_state = int(request.get("random_state", 7))
    max_train_rows = max(10_000, min(300_000, int(request.get("max_train_rows", 150_000))))

    frame = dataset.dropna(subset=[*feature_columns]).copy()
    frame = frame.sort_values(["signal_date", "candidate_rank", "instrument_id"])
    dates = list(pd.Index(frame["signal_date"].dropna().unique()).sort_values())
    date_number = {value: index for index, value in enumerate(dates)}
    prediction_parts: list[pd.DataFrame] = []
    model = None
    fitted_at = -1
    importance: list[dict[str, Any]] = []
    fits = 0

    for current_index, current_date in enumerate(dates):
        current = frame[frame["signal_date"] == current_date].copy()
        eligible = frame[
            (frame["signal_date"] < current_date)
            & (frame["maturity_date"] <= current_date)
            & (frame["signal_date"] >= dates[max(0, current_index - train_window)])
        ].dropna(subset=["forward_return", *feature_columns])
        if len(eligible) > max_train_rows:
            eligible = eligible.tail(max_train_rows)
        should_fit = model is None or current_index - fitted_at >= retrain_every
        if should_fit and eligible["signal_date"].nunique() >= min_train_dates:
            ages = current_index - eligible["signal_date"].map(date_number).to_numpy()
            sample_weight = np.power(0.5, ages / max(20.0, train_window / 3.0))
            model = _make_model(model_id, random_state)
            model.fit(
                eligible[feature_columns].to_numpy(dtype=float),
                eligible["forward_return"].clip(-0.30, 0.30).to_numpy(dtype=float),
                sample_weight=sample_weight,
            )
            fitted_at = current_index
            fits += 1
            importance = _feature_importance(model, feature_columns)
        current["prediction"] = np.nan
        if model is not None and not current.empty:
            current["prediction"] = model.predict(current[feature_columns].to_numpy(dtype=float))
        prediction_parts.append(current)

    predicted = pd.concat(prediction_parts, ignore_index=True) if prediction_parts else frame.head(0)
    valid = predicted.dropna(subset=["prediction", "forward_return"])
    daily_ic = valid.groupby("signal_date").apply(
        lambda group: group["prediction"].corr(group["forward_return"], method="spearman")
        if len(group) >= 10 else np.nan,
        include_groups=False,
    ).dropna()
    ic_std = float(daily_ic.std(ddof=1)) if len(daily_ic) > 1 else 0.0
    summary = {
        "model": model_id,
        "name": model_meta["name"],
        "train_window": train_window,
        "retrain_every": retrain_every,
        "fits": fits,
        "predictions": int(predicted["prediction"].notna().sum()),
        "dates": int(predicted.loc[predicted["prediction"].notna(), "signal_date"].nunique()),
        "rank_ic": float(daily_ic.mean()) if not daily_ic.empty else None,
        "icir": float(daily_ic.mean() / ic_std) if ic_std > 0 else None,
        "positive_rate": float((daily_ic > 0).mean()) if not daily_ic.empty else None,
        "feature_importance": importance,
        "daily_ic": [
            {"date": pd.Timestamp(index).date().isoformat(), "value": float(value)}
            for index, value in daily_ic.items()
        ],
    }
    return predicted, summary


def _model_backtest(
    predicted: pd.DataFrame,
    pipeline: PipelineSpec,
    request: dict[str, Any],
) -> dict[str, Any] | None:
    positions = max(1, min(30, int(request.get("positions", 8))))
    rebalance = max(1, min(60, int(request.get("rebalance_every", 10))))
    rows = predicted.dropna(subset=["prediction"]).copy()
    if rows.empty:
        return None
    rows["candidate_rank"] = rows.groupby("signal_date")["prediction"].rank(
        method="first", ascending=False
    )
    candidates = rows[rows["candidate_rank"] <= positions].copy()
    candidates = candidates.rename(columns={"prediction": "score"})
    if "liquidity20" not in candidates and "adv_20" in candidates:
        candidates = candidates.rename(columns={"adv_20": "liquidity20"})
    required = [
        "signal_date", "instrument_id", "code", "name", "exchange",
        "signal_close", "score", "candidate_rank", "liquidity20",
    ]
    candidates = candidates[required]

    config = BacktestConfig(
        strategy="momentum", codes=[],
        start=pd.Timestamp(pipeline.start).date(), end=pd.Timestamp(pipeline.end).date(),
        universe_scope="all" if pipeline.scope in {"all", "bj"} else "sh_sz",
        initial_cash=pipeline.initial_cash, execution_mode="next_open",
        commission=0.0003, min_commission=5.0, transfer_fee=0.00001,
        stamp_tax=0.0005, slippage=0.0005, invest_ratio=0.80,
        lot_size=100, max_participation=0.05,
        params={"lookback": 20, "top_n": positions, "rebalance": rebalance},
    )
    return run_full_market_backtest(
        config,
        scope="all" if pipeline.scope in {"all", "bj"} else "sh_sz",
        top_n=positions,
        rebalance=rebalance,
        persist=False,
        _candidates=candidates,
        _active_gate_exit_signal_dates=None,
        _max_position_pct=0.10,
    )


def run_factor_lab(request: dict[str, Any]) -> dict[str, Any]:
    selected = _selected_factors(request)
    pipeline = _build_pipeline(request, selected)
    baseline = run_lego_backtest(pipeline, persist=False)
    details = baseline.get("rank_details", pd.DataFrame()).copy()
    model_result = None
    model_summary = None
    if bool(request.get("run_model", True)) and not details.empty:
        feature_columns = [f"{item['block_id']}__score" for item in selected]
        horizon = max(1, min(60, int(request.get("label_horizon", 5))))
        dataset = _attach_execution_labels(details, horizon, pipeline.end)
        predicted, model_summary = _walk_forward_model(dataset, feature_columns, request)
        model_result = _model_backtest(predicted, pipeline, request)

    return {
        "pipeline": asdict(pipeline),
        "selected_factors": selected,
        "baseline": baseline,
        "model_summary": model_summary,
        "model_result": model_result,
        "notes": {
            "factor_engine": "DuckDB横截面PIT计算",
            "model_universe": f"每日因子候选池Top {int(request.get('research_pool', 300))}",
            "label": f"T+1开盘至第{int(request.get('label_horizon', 5)) + 1}个开盘的成本后收益",
            "warning": "历史ST状态和历史行业成分仍非完整PIT；机器学习结果只用于研究。",
        },
    }
