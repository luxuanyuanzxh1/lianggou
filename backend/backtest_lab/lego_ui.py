"""Streamlit UI for the modular ("Lego") strategy pipeline.

The module deliberately contains presentation and session-state code only.  The
pipeline contract, validation and backtest implementation live in
``backtest_lab.lego_pipeline`` so a saved pipeline is equally usable from a
notebook, a scheduled job or this page.
"""

from __future__ import annotations

import copy
import json
import re
import time
import uuid
from dataclasses import asdict, is_dataclass
from datetime import date
from numbers import Number
from typing import Any, Callable

import pandas as pd
import streamlit as st


STATE_PIPELINE = "lego_builder_pipeline"
STATE_RESULT = "lego_builder_result"
STATE_NOTICE = "lego_builder_notice"
STATE_WORKFLOW = "lego_builder_workflow"
STATE_WORKFLOW_OWNER = "lego_builder_workflow_owner"


STAGE_ORDER = [
    "universe",
    "filter",
    "strategy",
    "fundamental",
    "factor",
    "normalize",
    "penalty",
    "rank",
    "portfolio",
    "execution",
]

STAGE_META = {
    "universe": ("① 股票池", "确定每天从哪些证券开始筛选"),
    "filter": ("② 硬筛", "先剔除 ST、停牌、不可买涨停、流动性不足等不可交易标的"),
    "strategy": ("③ 策略初选", "用技术或交易规则缩小候选范围；同组可用 AND / OR 拼接"),
    "fundamental": ("④ 基本面 / AI（占位）", "只允许使用当时已披露的信息；数据未接好前不会参与回测"),
    "factor": ("⑤ 多因子", "一个因子就是一块积木，逐项设置方向、权重和变换"),
    "normalize": ("⑥ 归一化", "去极值、标准化或 S 形映射，让不同量纲可以比较"),
    "penalty": ("⑦ 风险惩罚", "从总分中扣除波动、回撤、集中度等风险；未知公式保持禁用"),
    "rank": ("⑧ 每日排名", "按最终分从高到低产生当日 1、2、3……名"),
    "portfolio": ("⑨ 组合与仓位", "把排名转换成持仓数量、权重、调仓频率和现金比例"),
    "execution": ("⑩ A股撮合", "次日成交、T+1、整手、涨跌停、停牌、费用和滑点"),
}

FALLBACK_IO = {
    "universe": ("本地证券目录与点时行情", "当日可扫描股票池"),
    "filter": ("上一块候选股票", "通过硬条件的候选股票"),
    "strategy": ("硬筛后的行情序列", "满足策略信号的候选股票"),
    "fundamental": ("点时财报、公告与候选股票", "基本面特征或解释性分数"),
    "factor": ("候选股票与一个原始因子", "该因子的方向化分数"),
    "normalize": ("各因子原始分", "可比较的标准分 / S 形分"),
    "penalty": ("标准化总分与风险特征", "扣除风险后的最终分"),
    "rank": ("最终分", "每天按分数排列的候选清单"),
    "portfolio": ("每日排名与当前持仓", "目标股票及目标权重"),
    "execution": ("目标仓位与可成交行情", "符合 A 股规则的订单、成交和权益"),
}


def _plain(value: Any) -> Any:
    """Convert dataclasses and nested containers into editable plain values."""

    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (date, pd.Timestamp)):
        return value.isoformat()
    return value


def _ensure_pipeline(value: Any) -> dict[str, Any]:
    raw = _plain(value)
    if isinstance(raw, list):
        raw = {"blocks": raw}
    if not isinstance(raw, dict):
        raise TypeError("策略流水线必须是 PipelineSpec 或字典")
    pipeline = copy.deepcopy(raw)
    pipeline.setdefault("id", uuid.uuid4().hex[:12])
    pipeline.setdefault("name", "我的乐高策略")
    pipeline.setdefault("description", "每天依次筛选、打分、排名、组合买入并按 A 股规则回测。")
    pipeline.setdefault("version", 1)
    pipeline.setdefault("scope", "all")
    pipeline.setdefault("start", "2020-01-01")
    pipeline.setdefault("end", date.today().isoformat())
    pipeline.setdefault("initial_cash", 1_000_000.0)
    pipeline.setdefault("fees", {})
    blocks = pipeline.setdefault("blocks", [])
    if not isinstance(blocks, list):
        raise TypeError("pipeline.blocks 必须是列表")
    used_ids: set[str] = set()
    for index, item in enumerate(blocks):
        if not isinstance(item, dict):
            item = _plain(item)
            blocks[index] = item
        block_id = str(item.get("id") or uuid.uuid4().hex[:12])
        if block_id in used_ids:
            block_id = uuid.uuid4().hex[:12]
        used_ids.add(block_id)
        item["id"] = block_id
        item.setdefault("type", "unknown")
        item.setdefault("category", "strategy")
        item.setdefault("name", item["type"])
        item.setdefault("enabled", True)
        item.setdefault("combine", "AND")
        item.setdefault("params", {})
    return pipeline


def _catalog_dict(catalog: Any) -> dict[str, dict[str, Any]]:
    if isinstance(catalog, dict):
        return {str(key): (_plain(value) if not isinstance(value, dict) else copy.deepcopy(value)) for key, value in catalog.items()}
    result: dict[str, dict[str, Any]] = {}
    for item in catalog or []:
        meta = _plain(item)
        key = str(meta.get("type") or meta.get("id") or "")
        if key:
            result[key] = meta
    return result


def _semantic_stage(block_or_meta: dict[str, Any], type_id: str = "") -> str:
    """Split the backend's broad factor group into beginner-friendly stages."""

    category = str(block_or_meta.get("category", "strategy")).lower()
    text = " ".join(
        [
            type_id,
            str(block_or_meta.get("type", "")),
            str(block_or_meta.get("name", "")),
            category,
        ]
    ).lower()
    if category in {"universe", "filter", "strategy", "penalty", "rank", "portfolio", "execution"}:
        return category
    if category in {"fundamental", "fundamental_ai", "ai"} or any(token in text for token in ("fundamental", "基本面", "ai_score", "llm")):
        return "fundamental"
    if category in {"normalize", "normalization", "transform"} or any(token in text for token in ("normalize", "normalise", "winsor", "sigmoid", "s_shape", "standardize", "归一", "标准化", "去极")):
        return "normalize"
    if category in {"factor", "score", "multifactor"}:
        return "factor"
    return category if category in STAGE_META else "strategy"


def _schema_defaults(meta: dict[str, Any]) -> dict[str, Any]:
    raw = meta.get("params", meta.get("parameters", {})) or {}
    explicit = meta.get("default_params", {}) or {}
    result: dict[str, Any] = {}
    if isinstance(raw, dict):
        for key, schema in raw.items():
            if isinstance(schema, dict) and "default" in schema:
                result[key] = copy.deepcopy(schema["default"])
            elif not isinstance(schema, dict):
                result[key] = copy.deepcopy(schema)
    result.update(copy.deepcopy(explicit))
    return result


def _new_block(type_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    disabled_reason = meta.get("disabled_reason")
    enabled_by_catalog = meta.get("enabled", meta.get("available", True))
    return {
        "id": uuid.uuid4().hex[:12],
        "type": type_id,
        "category": meta.get("category", "strategy"),
        "name": meta.get("name", meta.get("label", type_id)),
        "enabled": bool(enabled_by_catalog and not disabled_reason),
        "combine": "AND",
        "params": _schema_defaults(meta),
    }


def _insert_block(pipeline: dict[str, Any], block: dict[str, Any]) -> None:
    """Insert after the selected stage while preserving a valid default order."""

    blocks = pipeline["blocks"]
    wanted = STAGE_ORDER.index(_semantic_stage(block, str(block.get("type", ""))))
    insertion = 0
    for index, existing in enumerate(blocks):
        stage = _semantic_stage(existing, str(existing.get("type", "")))
        stage_index = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else len(STAGE_ORDER)
        if stage_index <= wanted:
            insertion = index + 1
    blocks.insert(insertion, block)


def _parse_day(value: Any, fallback: date) -> date:
    try:
        return pd.Timestamp(value).date()
    except Exception:
        return fallback


def _status(meta: dict[str, Any], type_id: str) -> tuple[str, str, str]:
    """Return status label, explanation and CSS class without overstating readiness."""

    reason = str(meta.get("disabled_reason") or "").strip()
    status = str(meta.get("data_status") or meta.get("status") or "").lower()
    text = " ".join([type_id, str(meta.get("name", "")), str(meta.get("description", ""))]).lower()
    # Never use a raw ``"ai" in text`` test: words such as ``daily`` and
    # ``available`` contain those letters and would incorrectly disable a
    # perfectly normal daily-price block.
    ai_marker = bool(re.search(r"(?:^|[_\-\s/])(?:ai|llm)(?:$|[_\-\s/])", text))
    fundamental_marker = "fundamental" in text or "基本面" in text
    delta_marker = any(token in text for token in ("deltarp", "delta_rp", "delta rp"))
    minute_marker = "14:45" in text or "1445" in text
    sensitive = fundamental_marker or ai_marker or delta_marker or minute_marker
    unavailable = meta.get("enabled") is False or meta.get("available") is False or status in {"missing", "unavailable", "not_ready", "disabled"}
    if reason or unavailable or sensitive:
        if not reason:
            if fundamental_marker or ai_marker:
                reason = "点时基本面 / 公告数据和可复现 AI 模型尚未接入，当前只作禁用占位。"
            elif delta_marker:
                reason = "Delta RP 的确切公式尚未提供，当前禁用，绝不用猜测公式代替。"
            elif minute_marker:
                reason = "只有分钟覆盖校验通过后才可运行；缺失日期不会用日线价格替代。"
            else:
                reason = "所需数据尚未就绪。"
        return "数据未就绪", reason, "lego-status-warn"
    requirement = meta.get("data_requirement") or meta.get("data") or meta.get("required_data")
    if isinstance(requirement, list):
        requirement = "、".join(map(str, requirement))
    if status in {"ready", "available", "ok"}:
        return "数据可用", str(requirement or "后端已声明该积木的数据可用。"), "lego-status-ready"
    if meta.get("available") is True and requirement:
        return "数据可用", f"已接入：{requirement}；运行时仍会逐日校验覆盖。", "lego-status-ready"
    return "运行时校验", str(requirement or "运行前由后端检查日期覆盖和字段完整性。"), "lego-status-neutral"


def _render_styles() -> None:
    st.markdown(
        """
        <style>
        .lego-intro{padding:14px 16px;border:1px solid #345064;background:linear-gradient(135deg,#111e27,#0d171d);margin-bottom:12px}
        .lego-intro b{color:#d9edf8}.lego-intro span{display:block;color:#8fa8b6;margin-top:5px;line-height:1.6}
        .lego-stage{padding:10px 13px;margin:18px 0 8px;border-left:5px solid #52a7d8;background:#101c24;color:#d9e9f2}
        .lego-stage b{font-size:17px}.lego-stage span{display:block;color:#829ba9;font-size:12px;margin-top:4px}
        .lego-arrow{text-align:center;color:#6696b1;padding:3px 0 5px;font-size:13px;letter-spacing:.03em}
        .lego-io{font-size:12px;color:#91a7b3;line-height:1.65;padding:8px 10px;background:#0d171d;border:1px solid #243641}
        .lego-status-ready,.lego-status-warn,.lego-status-neutral{display:inline-block;padding:3px 8px;margin:3px 0 6px;font-size:12px;font-weight:700;border:1px solid}
        .lego-status-ready{color:#75d9a8;border-color:#2d7555;background:#10271c}
        .lego-status-warn{color:#e7ba76;border-color:#76552c;background:#2a1f13}
        .lego-status-neutral{color:#93b9cc;border-color:#355a6d;background:#10212a}
        .lego-disabled{color:#81909a;font-size:12px;padding:7px 9px;border-left:3px solid #936d38;background:#211a12}
        .workflow-help{padding:10px 12px;border:1px solid #345064;background:#0d171d;color:#91a7b3;line-height:1.65;margin-bottom:10px}
        .workflow-help b{color:#d9edf8}.workflow-panel-title{font-weight:800;color:#d9edf8;margin:3px 0 9px}
        .workflow-edge{padding:9px 10px;border:1px solid #35556a;background:#10202a;color:#a8c7d8;margin-bottom:8px}
        [data-testid="stVerticalBlockBorderWrapper"]{background:#10191f;border-color:#30434e!important}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _param_definition(meta: dict[str, Any], key: str) -> dict[str, Any]:
    raw = meta.get("params", meta.get("parameters", {})) or {}
    definition = raw.get(key, {}) if isinstance(raw, dict) else {}
    return definition if isinstance(definition, dict) else {"default": definition}


def _edit_param(block: dict[str, Any], key: str, value: Any, definition: dict[str, Any]) -> Any:
    label = str(definition.get("label") or definition.get("name") or {
        "weight": "因子权重",
        "direction": "因子方向",
        "transform": "分数变换",
        "method": "分数变换",
        "sigmoid_k": "S 形斜率",
        "clip": "标准化截断值",
        "winsorize": "去极值方法",
        "start_one": "单日启动涨幅（%）",
        "start_two": "两日启动涨幅（%）",
        "exit_one": "单日退出跌幅（%）",
        "ma_period": "退出均线周期",
        "max_gap_days": "活跃市值最大补齐间隔（日）",
        "commission": "佣金率",
        "min_commission": "最低佣金（元）",
        "transfer_fee": "过户费率",
        "stamp_tax": "卖出印花税率",
        "slippage": "基础滑点",
        "lot_size": "整手股数",
        "invest_ratio": "最大资金使用率",
        "max_participation": "最大成交参与率",
        "lookback": "回看周期",
        "threshold": "阈值",
        "top_n": "保留前 N 名",
        "rebalance": "调仓间隔（交易日）",
    }.get(key, key))
    help_text = definition.get("help") or definition.get("description") or f"传给后端参数：{key}"
    widget_key = f"lego-param-{block['id']}-{key}"
    kind = str(definition.get("type") or "").lower()
    options = definition.get("options") or definition.get("choices")

    if key == "direction" and not options:
        options = ["higher", "lower"]
    if key == "method" and not options:
        options = ["robust_sigmoid", "percentile"]
    if key == "winsorize" and not options:
        options = ["median_mad"]
    if key == "weighting" and not options:
        options = ["equal"]
    if key == "mode" and not options:
        options = ["next_open"] if value == "next_open" else [value]
    if key == "transform" and not options:
        options = ["none", "rank", "zscore", "sigmoid"]
    if options:
        options = list(options)
        if value not in options:
            options = [value] + options if value is not None else options
        formatter = None
        if key == "direction":
            formatter = lambda item: {"higher": "越大越好（正向）", "lower": "越小越好（反向）", 1: "越大越好（正向）", -1: "越小越好（反向）"}.get(item, str(item))
        elif key in {"transform", "method"}:
            formatter = lambda item: {
                "none": "不变换",
                "rank": "截面排名",
                "zscore": "Z-score",
                "sigmoid": "S 形映射",
                "robust_sigmoid": "稳健 S 形（中位数 / MAD）",
                "percentile": "截面百分位排名",
            }.get(item, str(item))
        elif key == "winsorize":
            formatter = lambda item: {"median_mad": "中位数 / MAD 稳健去极值"}.get(item, str(item))
        elif key == "weighting":
            formatter = lambda item: {"equal": "等权"}.get(item, str(item))
        elif key == "mode":
            formatter = lambda item: {"next_open": "信号冻结后，下一交易日开盘"}.get(item, str(item))
        return st.selectbox(label, options, index=max(0, options.index(value)) if value in options else 0, format_func=formatter, help=help_text, key=widget_key)
    if isinstance(value, bool) or kind in {"bool", "boolean"}:
        return st.checkbox(label, value=bool(value), help=help_text, key=widget_key)
    if isinstance(value, int) and not isinstance(value, bool) or kind in {"int", "integer"}:
        numeric_value = value if value is not None else definition.get("default", 0)
        kwargs: dict[str, Any] = {"value": int(numeric_value), "step": int(definition.get("step", 1)), "help": help_text, "key": widget_key}
        if definition.get("min") is not None:
            kwargs["min_value"] = int(definition["min"])
        if definition.get("max") is not None:
            kwargs["max_value"] = int(definition["max"])
        if key in {"lot_size", "ma_period"}:
            kwargs.setdefault("min_value", 1)
        elif key == "max_gap_days":
            kwargs.setdefault("min_value", 0)
        return int(st.number_input(label, **kwargs))
    if isinstance(value, float) or kind in {"float", "number", "decimal"}:
        numeric_value = value if value is not None else definition.get("default", 0.0)
        kwargs = {"value": float(numeric_value), "step": float(definition.get("step", 0.01)), "help": help_text, "key": widget_key}
        if definition.get("min") is not None:
            kwargs["min_value"] = float(definition["min"])
        if definition.get("max") is not None:
            kwargs["max_value"] = float(definition["max"])
        if key in {"commission", "transfer_fee", "stamp_tax", "slippage"}:
            kwargs.update({"min_value": 0.0, "step": 0.00001, "format": "%.5f"})
        elif key == "min_commission":
            kwargs.update({"min_value": 0.0, "step": 1.0})
        elif key in {"invest_ratio", "max_participation"}:
            kwargs.update({"min_value": 0.01, "max_value": 1.0, "step": 0.01})
        elif key == "sigmoid_k":
            kwargs.update({"min_value": 0.01, "max_value": 20.0, "step": 0.1})
        elif key == "clip":
            kwargs.update({"min_value": 0.5, "max_value": 20.0, "step": 0.5})
        return float(st.number_input(label, **kwargs))
    if isinstance(value, list) or kind in {"list", "array", "multiselect"}:
        raw = st.text_area(label, value=", ".join(map(str, value or [])), help=f"{help_text}；多个值用逗号分隔。", key=widget_key, height=70)
        return [item.strip() for item in raw.replace("，", ",").split(",") if item.strip()]
    if isinstance(value, dict) or kind in {"json", "object"}:
        raw = st.text_area(label, value=json.dumps(value or {}, ensure_ascii=False, indent=2), help=help_text, key=widget_key, height=110)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            st.caption("JSON 尚未合法，继续保留上一次有效值。")
            return value
    if kind in {"textarea", "code"}:
        return st.text_area(label, value=str(value or ""), help=help_text, key=widget_key, height=90)
    return st.text_input(label, value=str(value or ""), help=help_text, key=widget_key)


def _render_block_params(block: dict[str, Any], meta: dict[str, Any], semantic_stage: str) -> None:
    params = block.setdefault("params", {})
    schema = meta.get("params", meta.get("parameters", {})) or {}
    keys = list(schema) if isinstance(schema, dict) else []
    for key in params:
        if key not in keys:
            keys.append(key)
    # A factor remains an atomic Lego brick, but all three scoring decisions are
    # visible on that brick.  They are never hidden in a fixed factor table.
    if semantic_stage == "factor":
        transform_key = "method" if "method" in params or "method" in keys else "transform"
        if transform_key == "method":
            # Earlier UI drafts used a visual-only ``transform`` key.  It must
            # not survive beside the executable backend ``method`` parameter.
            params.pop("transform", None)
            keys = [key for key in keys if key != "transform"]
        factor_defaults = {"weight": 1.0, "direction": "higher", transform_key: "robust_sigmoid"}
        for key, default in factor_defaults.items():
            params.setdefault(key, default)
            if key not in keys:
                keys.append(key)
    if not keys:
        st.caption("这个积木没有可调参数；启停或调整它在流水线中的位置即可。")
        return
    if semantic_stage == "universe":
        # Scope and custom codes are edited once on the baseplate.  Rendering a
        # second widget for the same values would let stale Streamlit widget
        # state overwrite the visible baseplate selection on the next rerun.
        keys = [key for key in keys if key not in {"scope", "codes"}]
        st.caption("市场范围和自定义代码请在上方“回测底板积木”统一设置。")
    for key in keys:
        definition = _param_definition(meta, key)
        value = params.get(key, definition.get("default"))
        params[key] = _edit_param(block, key, value, definition)


def _funnel_lookup(result: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not result:
        return {}
    funnel = result.get("stage_funnel")
    if funnel is None:
        funnel = result.get("funnel")
    if funnel is None:
        funnel = []
    if isinstance(funnel, pd.DataFrame):
        funnel = funnel.to_dict("records")
    if isinstance(funnel, dict):
        rows = []
        for key, value in funnel.items():
            row = copy.deepcopy(value) if isinstance(value, dict) else {"output": value}
            row.setdefault("key", key)
            rows.append(row)
    else:
        rows = list(funnel or [])
    lookup: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ("block_id", "id", "type", "stage", "category", "key"):
            if row.get(key) is not None:
                lookup[str(row[key])] = row
        for block_id in row.get("block_ids", []) or []:
            lookup[str(block_id)] = row
    return lookup


def _count_value(row: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if row.get(name) is not None:
            return row[name]
    return None


def _render_arrow(block: dict[str, Any], funnel: dict[str, dict[str, Any]]) -> None:
    row = funnel.get(str(block.get("id"))) or funnel.get(str(block.get("type"))) or funnel.get(str(block.get("category")))
    if row:
        before = _count_value(row, ("before_instruments", "input_instruments"))
        after = _count_value(row, ("after_instruments", "output_instruments"))
        unit = "只股票"
        if before is None and after is None:
            before = _count_value(row, ("input_count", "before", "input", "universe_size"))
            after = _count_value(row, ("output_count", "after", "output", "remaining", "count"))
            unit = "条样本"
        before_text = f"{int(before):,}" if isinstance(before, Number) and not pd.isna(before) else "—"
        after_text = f"{int(after):,}" if isinstance(after, Number) and not pd.isna(after) else "—"
        message = f"↓ 数据沙漏：{before_text} {unit} → {after_text} {unit}"
    else:
        message = "↓ 数据沙漏：输入 — 只 → 输出 — 只（运行后显示真实数量）"
    st.markdown(f'<div class="lego-arrow">{message}</div>', unsafe_allow_html=True)


def _render_baseplate(pipeline: dict[str, Any], db: dict[str, Any]) -> None:
    st.markdown('<div class="lego-stage"><b>回测底板积木</b><span>策略名称、样本区间和初始资金；所有筛选积木都扣在这块底板上。</span></div>', unsafe_allow_html=True)
    with st.container(border=True):
        col1, col2 = st.columns([1.1, 1.9])
        pipeline["name"] = col1.text_input("流水线名称", value=str(pipeline.get("name", "我的乐高策略")), help="保存后可再次载入，也会写入本次回测结果。", key="lego-pipeline-name")
        pipeline["description"] = col2.text_input("一句话说明", value=str(pipeline.get("description", "")), help="写清楚这套策略想筛出什么股票。", key="lego-pipeline-description")

        min_day = _parse_day(db.get("min_date"), date(2000, 1, 1))
        max_day = _parse_day(db.get("max_date"), date.today())
        start_day = max(min_day, min(_parse_day(pipeline.get("start"), min_day), max_day))
        end_day = max(start_day, min(_parse_day(pipeline.get("end"), max_day), max_day))
        c1, c2, c3, c4 = st.columns(4)
        scope_options = ["all", "sh_sz", "custom"]
        scope = pipeline.get("scope", "all")
        if scope not in scope_options:
            scope = "all"
        pipeline["scope"] = c1.selectbox(
            "市场范围",
            scope_options,
            index=scope_options.index(scope),
            format_func=lambda item: {"all": "沪深京全市场", "sh_sz": "沪深全市场", "custom": "自定义代码"}[item],
            help="真正全市场模式会在 DuckDB 中向量化扫描，不会把数千只股票同时塞入事件引擎。",
            key="lego-pipeline-scope",
        )
        picked = c2.date_input("开始日期", value=start_day, min_value=min_day, max_value=max_day, help="训练或指标回看窗口由各积木自行向前读取。", key="lego-pipeline-start")
        ended = c3.date_input("结束日期", value=end_day, min_value=min_day, max_value=max_day, help="当天数据不完整时，后端会剔除不完整交易日。", key="lego-pipeline-end")
        pipeline["start"], pipeline["end"] = picked.isoformat(), ended.isoformat()
        pipeline["initial_cash"] = float(c4.number_input("初始资金", min_value=100_000.0, value=float(pipeline.get("initial_cash", 1_000_000)), step=100_000.0, help="仅用于模拟/历史回测，不连接实盘账户。", key="lego-pipeline-cash"))
        universe_block = next(
            (block for block in pipeline.get("blocks", []) if str(block.get("category", "")).lower() == "universe"),
            None,
        )
        if universe_block is not None:
            universe_block.setdefault("params", {})["scope"] = pipeline["scope"]
        if pipeline["scope"] == "custom" and universe_block is not None:
            universe_params = universe_block["params"]
            raw_codes = st.text_area(
                "自定义股票代码",
                value=", ".join(map(str, universe_params.get("codes", []))),
                placeholder="例如：600519, 000858, 300750",
                help="多个六位代码用逗号分隔；代码直接保存在股票池积木中，后端会逐一与本地证券目录核对。",
                key="lego-pipeline-custom-codes",
                height=72,
            )
            universe_params["codes"] = [
                item.strip().split(".")[0].zfill(6)
                for item in raw_codes.replace("，", ",").split(",")
                if item.strip()
            ]
        elif pipeline["scope"] == "custom":
            st.warning("自定义代码需要先添加一块“股票池”积木，然后才有地方保存代码。")
        st.markdown('<div class="lego-io">输入：本地只读行情数据库　→　输出：带有市场范围、样本期和模拟资金的回测任务</div>', unsafe_allow_html=True)


def _render_add_block(pipeline: dict[str, Any], catalog: dict[str, dict[str, Any]]) -> None:
    st.subheader("积木仓库")
    st.caption("每次只添加一个原子积木。多因子请逐项添加，每项因子都有自己的权重、方向和变换。")
    stages_with_items = [stage for stage in STAGE_ORDER if any(_semantic_stage(meta, type_id) == stage for type_id, meta in catalog.items())]
    if not stages_with_items:
        st.warning("积木目录为空。")
        return
    c1, c2, c3 = st.columns([0.8, 1.4, 0.45])
    selected_stage = c1.selectbox("积木类别", stages_with_items, format_func=lambda item: STAGE_META[item][0], help="选择要添加到流水线的环节。", key="lego-add-stage")
    choices = [type_id for type_id, meta in catalog.items() if _semantic_stage(meta, type_id) == selected_stage]
    selected_type = c2.selectbox("具体积木", choices, format_func=lambda item: str(catalog[item].get("name", catalog[item].get("label", item))), help="添加后可单独设置参数、启停和顺序。", key="lego-add-type")
    selected_meta = catalog[selected_type]
    selected_label, selected_status, selected_css = _status(selected_meta, selected_type)
    st.markdown(
        f'<span class="{selected_css}">{selected_label}</span>　'
        f'<span style="color:#8095a1;font-size:12px">{selected_meta.get("description", "")} {selected_status}</span>',
        unsafe_allow_html=True,
    )
    if c3.button("＋ 添加", type="primary", use_container_width=True, help="按标准环节顺序插入；之后仍可用上下按钮调整。"):
        _insert_block(pipeline, _new_block(selected_type, catalog[selected_type]))
        st.session_state[STATE_PIPELINE] = pipeline
        st.session_state[STATE_NOTICE] = f"已添加：{catalog[selected_type].get('name', selected_type)}"
        st.rerun()


def _render_stage_overview(pipeline: dict[str, Any]) -> None:
    """Always show every supported stage, including intentionally empty ones."""

    blocks = pipeline.get("blocks", [])
    counts = {stage: 0 for stage in STAGE_ORDER}
    for block in blocks:
        stage = _semantic_stage(block, str(block.get("type", "")))
        if stage in counts:
            counts[stage] += 1
    st.subheader("整条流水线")
    st.caption("0 块代表这个环节尚未添加；多因子是一项一块，归一化规则本身也是独立积木。")
    for offset in range(0, len(STAGE_ORDER), 5):
        columns = st.columns(5)
        for column, stage in zip(columns, STAGE_ORDER[offset : offset + 5]):
            label = STAGE_META[stage][0].split(" ", 1)[-1]
            column.metric(label, f"{counts[stage]} 块")


def _render_pipeline_blocks(pipeline: dict[str, Any], catalog: dict[str, dict[str, Any]], result: dict[str, Any] | None) -> None:
    blocks = pipeline["blocks"]
    funnel = _funnel_lookup(result)
    if not blocks:
        st.warning("流水线还是空的。请从积木仓库添加股票池、筛选、排名、组合和执行积木。")
        return
    last_stage = None
    for index, block in enumerate(list(blocks)):
        type_id = str(block.get("type", "unknown"))
        meta = catalog.get(type_id, {})
        stage = _semantic_stage(block if block.get("category") else meta, type_id)
        if stage != last_stage:
            title, explanation = STAGE_META.get(stage, (stage, ""))
            st.markdown(f'<div class="lego-stage"><b>{title}</b><span>{explanation}</span></div>', unsafe_allow_html=True)
            last_stage = stage

        with st.container(border=True):
            title_col, enabled_col, logic_col, up_col, down_col, delete_col = st.columns([2.8, 0.8, 0.85, 0.34, 0.34, 0.42])
            display_name = str(block.get("name") or meta.get("name") or type_id)
            title_col.markdown(f"**🧱 {display_name}**  \n`{type_id}`")
            status_label, status_text, css_class = _status(meta, type_id)
            catalog_unavailable = (
                bool(meta.get("disabled_reason"))
                or meta.get("enabled") is False
                or meta.get("available") is False
                or status_label == "数据未就绪"
            )
            if catalog_unavailable:
                block["enabled"] = False
            block["enabled"] = enabled_col.toggle(
                "启用",
                value=bool(block.get("enabled", True)),
                disabled=catalog_unavailable,
                key=f"lego-enabled-{block['id']}",
                help="关闭后保留配置但运行时跳过。目录标记为未就绪的积木不能启用假运行。",
            )
            if stage in {"filter", "strategy", "fundamental", "factor", "penalty"}:
                combine = str(block.get("combine", "AND")).upper()
                if combine not in {"AND", "OR"}:
                    combine = "AND"
                block["combine"] = logic_col.selectbox("连接", ["AND", "OR"], index=0 if combine == "AND" else 1, key=f"lego-combine-{block['id']}", help="AND：同时满足；OR：满足任一。第一块的连接符会由后端忽略。")
            else:
                logic_col.caption("顺序连接")
            if up_col.button("↑", key=f"lego-up-{block['id']}", disabled=index == 0, help="上移一格；若打乱类型顺序，校验器会明确提示。"):
                blocks[index - 1], blocks[index] = blocks[index], blocks[index - 1]
                st.session_state[STATE_PIPELINE] = pipeline
                st.rerun()
            if down_col.button("↓", key=f"lego-down-{block['id']}", disabled=index == len(blocks) - 1, help="下移一格；允许尝试，但非法顺序不能运行。"):
                blocks[index + 1], blocks[index] = blocks[index], blocks[index + 1]
                st.session_state[STATE_PIPELINE] = pipeline
                st.rerun()
            if delete_col.button("×", key=f"lego-delete-{block['id']}", help="从流水线删除这一块；保存前仍会重新校验必需环节。"):
                del blocks[index]
                st.session_state[STATE_PIPELINE] = pipeline
                st.rerun()

            description = meta.get("description") or block.get("description") or "该积木的详细计算由后端目录定义。"
            st.caption(str(description))
            st.markdown(f'<span class="{css_class}">{status_label}</span>　<span style="color:#8095a1;font-size:12px">{status_text}</span>', unsafe_allow_html=True)
            input_text = meta.get("input") or meta.get("inputs") or FALLBACK_IO[stage][0]
            output_text = meta.get("output") or meta.get("outputs") or FALLBACK_IO[stage][1]
            if isinstance(input_text, list):
                input_text = "、".join(map(str, input_text))
            if isinstance(output_text, list):
                output_text = "、".join(map(str, output_text))
            st.markdown(f'<div class="lego-io"><b>输入</b>：{input_text}<br><b>输出</b>：{output_text}</div>', unsafe_allow_html=True)
            with st.expander("参数怎么用 / 编辑参数", expanded=stage in {"factor", "normalize", "execution"}):
                _render_block_params(block, meta, stage)
            if not block["enabled"]:
                st.markdown('<div class="lego-disabled">这块积木当前已停用：配置会保存，但不会参与筛选、打分或交易。</div>', unsafe_allow_html=True)
        if index < len(blocks) - 1:
            _render_arrow(block, funnel)


def _validate_for_ui(pipeline: dict[str, Any], validate_pipeline: Callable[..., Any]) -> tuple[dict[str, Any] | None, str | None]:
    try:
        validated = validate_pipeline(copy.deepcopy(pipeline))
        return _ensure_pipeline(validated), None
    except Exception as exc:  # Validation messages are intended for the builder.
        return None, str(exc)


def _saved_label(row: dict[str, Any]) -> str:
    name = row.get("name") or row.get("pipeline_name") or "未命名流水线"
    identifier = row.get("id") or row.get("pipeline_id") or row.get("file") or ""
    return f"{name} · {identifier}" if identifier else str(name)


def _clear_widget_state() -> None:
    """Prevent old widget values from overwriting a newly loaded pipeline."""

    prefixes = (
        "lego-param-",
        "lego-enabled-",
        "lego-combine-",
        "lego-fee-",
        "lego-pipeline-",
        "lego-save-name",
        "workflow-",
        "lego-coze-workstation",
    )
    for key in list(st.session_state):
        if key.startswith(prefixes):
            del st.session_state[key]


def _render_save_load(pipeline: dict[str, Any], validate_pipeline: Callable[..., Any], save_pipeline: Callable[..., Any], list_pipelines: Callable[..., Any], load_pipeline: Callable[..., Any]) -> None:
    st.subheader("保存、加载与配置")
    save_col, load_col = st.columns(2)
    with save_col, st.container(border=True):
        st.markdown("**💾 保存整套乐高**")
        save_name = st.text_input("保存名称", value=str(pipeline.get("name", "我的乐高策略")), help="保存的是全部积木、顺序、启停状态和参数。", key="lego-save-name")
        if st.button("校验并保存", type="primary", use_container_width=True, help="顺序、必需积木、数据状态和参数均通过后才保存。"):
            validated, error = _validate_for_ui(pipeline, validate_pipeline)
            if error:
                st.error(f"不能保存：{error}")
            else:
                try:
                    validated["name"] = save_name.strip()
                    summary = save_pipeline(validated, save_name)
                    st.session_state[STATE_PIPELINE] = validated
                    st.success(f"已保存：{_saved_label(_plain(summary))}")
                except Exception as exc:
                    st.error(f"保存失败：{exc}")
    with load_col, st.container(border=True):
        st.markdown("**📂 载入已保存流水线**")
        try:
            saved = [_plain(item) for item in list_pipelines()]
        except Exception as exc:
            saved = []
            st.error(f"读取列表失败：{exc}")
        if saved:
            selected = st.selectbox("已保存策略", range(len(saved)), format_func=lambda index: _saved_label(saved[index]), help="载入会替换当前尚未保存的画布。", key="lego-load-choice")
            if st.button("载入到画布", use_container_width=True):
                identifier = saved[selected].get("id") or saved[selected].get("pipeline_id") or saved[selected].get("file")
                try:
                    _clear_widget_state()
                    st.session_state[STATE_PIPELINE] = _ensure_pipeline(load_pipeline(identifier))
                    st.session_state.pop(STATE_WORKFLOW, None)
                    st.session_state.pop(STATE_WORKFLOW_OWNER, None)
                    st.session_state.pop(STATE_RESULT, None)
                    st.rerun()
                except Exception as exc:
                    st.error(f"载入失败：{exc}")
        else:
            st.info("尚无已保存流水线。")
    with st.expander("查看当前配置 JSON（便于复核和版本管理）"):
        st.json(_plain(pipeline), expanded=2)
        st.download_button("下载配置 JSON", json.dumps(_plain(pipeline), ensure_ascii=False, indent=2).encode("utf-8"), file_name=f"{pipeline.get('id', 'lego-pipeline')}.json", mime="application/json", use_container_width=True)


def _render_funnel_summary(result: dict[str, Any], pipeline: dict[str, Any]) -> None:
    funnel = result.get("stage_funnel")
    if funnel is None:
        funnel = result.get("funnel")
    if funnel is None:
        return
    if isinstance(funnel, pd.DataFrame):
        table = funnel.copy()
    elif isinstance(funnel, dict):
        rows = []
        for key, value in funnel.items():
            row = copy.deepcopy(value) if isinstance(value, dict) else {"output": value}
            row.setdefault("stage", key)
            rows.append(row)
        table = pd.DataFrame(rows)
    else:
        table = pd.DataFrame(funnel)
    if table.empty:
        return
    st.subheader("本次真实数据沙漏")
    st.caption("数字来自本次回测，不是界面示例。可据此看出每一层淘汰了多少只股票。")
    st.dataframe(table, hide_index=True, use_container_width=True)


def _render_rank_details(result: dict[str, Any]) -> None:
    details = result.get("rank_details")
    if details is None:
        return
    if isinstance(details, pd.DataFrame):
        table = details.copy()
    else:
        table = pd.DataFrame(details)
    if table.empty:
        return
    st.subheader("每日排名与分项解释")
    st.caption("每只股票保留原始因子、变换后得分、权重、风险扣分与最终名次，方便检查为何入选。")
    day_column = next((column for column in ("date", "trade_date", "日期") if column in table.columns), None)
    if day_column:
        days = sorted(table[day_column].astype(str).unique(), reverse=True)
        selected_day = st.selectbox("查看排名日期", days, help="排名只使用该时点之前可见的数据。", key="lego-rank-day")
        table = table[table[day_column].astype(str) == selected_day]
    st.dataframe(table, hide_index=True, use_container_width=True, height=430)
    st.download_button("下载完整每日排名 CSV", pd.DataFrame(details).to_csv(index=False).encode("utf-8-sig"), file_name=f"{result.get('run_id', 'lego')}-ranks.csv", mime="text/csv")


def _state_to_workflow(state: Any) -> dict[str, Any]:
    """Persist only stable graph data; React Flow rendering options stay UI-only."""

    nodes = [
        {
            "id": str(node.id),
            "position": {
                "x": float(node.position.get("x", 0)),
                "y": float(node.position.get("y", 0)),
            },
        }
        for node in state.nodes
    ]
    edges = [
        {
            "id": str(edge.id),
            "source": str(edge.source),
            "target": str(edge.target),
            "label": str(edge.label or "数据流"),
        }
        for edge in state.edges
    ]
    return {"version": 1, "nodes": nodes, "edges": edges}


def _stage_color(stage: str) -> str:
    return {
        "universe": "#237b91",
        "filter": "#455a9f",
        "strategy": "#7a4fa3",
        "fundamental": "#8b5d8c",
        "factor": "#a36b35",
        "normalize": "#8b7831",
        "penalty": "#a14b4b",
        "rank": "#357e67",
        "portfolio": "#28756f",
        "execution": "#28639a",
    }.get(stage, "#425b6a")


def _flow_state_from_pipeline(
    pipeline: dict[str, Any], catalog: dict[str, dict[str, Any]], workflow: dict[str, Any] | None = None,
    selected_id: str | None = None,
) -> Any:
    from streamlit_flow import StreamlitFlowEdge, StreamlitFlowNode, StreamlitFlowState
    from backtest_lab.lego_workflow import default_workflow

    blocks = pipeline.get("blocks", [])
    graph = copy.deepcopy(workflow or pipeline.get("workflow") or default_workflow(blocks))
    node_positions = {
        str(node.get("id")): node.get("position", {})
        for node in graph.get("nodes", [])
        if isinstance(node, dict)
    }
    block_ids = {str(block["id"]) for block in blocks}
    if set(node_positions) != block_ids:
        graph = default_workflow(blocks)
        node_positions = {str(node["id"]): node["position"] for node in graph["nodes"]}

    nodes = []
    for index, block in enumerate(blocks):
        block_id = str(block["id"])
        type_id = str(block.get("type", "unknown"))
        meta = catalog.get(type_id, {})
        stage = _semantic_stage(block if block.get("category") else meta, type_id)
        stage_label = STAGE_META.get(stage, (stage, ""))[0].split(" ", 1)[-1]
        enabled = bool(block.get("enabled", True))
        ready = not bool(meta.get("disabled_reason")) and meta.get("available", True) is not False
        badge = "运行中" if enabled and ready else "停用" if not enabled else "待数据"
        content = f"{stage_label}\n{block.get('name') or meta.get('name') or type_id}\n{badge}"
        position = node_positions.get(block_id, {"x": 80 + index * 230, "y": 80})
        color = _stage_color(stage)
        nodes.append(StreamlitFlowNode(
            block_id,
            (float(position.get("x", 0)), float(position.get("y", 0))),
            {"content": content},
            node_type="input" if stage == "universe" else "output" if stage == "execution" else "default",
            source_position="right",
            target_position="left",
            draggable=True,
            selectable=True,
            connectable=True,
            deletable=False,
            style={
                "width": "190px",
                "minHeight": "78px",
                "padding": "12px 14px",
                "whiteSpace": "pre-line",
                "lineHeight": "1.5",
                "fontSize": "12px",
                "fontWeight": "700",
                "color": "#eaf5fa" if enabled else "#82939c",
                "backgroundColor": "#111c23" if enabled else "#12171a",
                "border": f"2px solid {color if enabled else '#465159'}",
                "borderRadius": "9px",
                "boxShadow": f"0 0 0 1px #071015, 0 6px 18px {color}33",
            },
        ))
    edges = []
    for index, edge in enumerate(graph.get("edges", []) or []):
        source, target = str(edge.get("source", "")), str(edge.get("target", ""))
        if source not in block_ids or target not in block_ids:
            continue
        edges.append(StreamlitFlowEdge(
            str(edge.get("id") or f"edge-{index}-{source}-{target}"),
            source,
            target,
            edge_type="smoothstep",
            marker_end={"type": "arrowclosed", "color": "#59b7e8"},
            animated=True,
            deletable=True,
            focusable=True,
            label=str(edge.get("label") or "数据流"),
            label_show_bg=True,
            label_bg_style={"fill": "#0a141a", "fillOpacity": 0.92},
            style={"stroke": "#59b7e8", "strokeWidth": 2},
        ))
    return StreamlitFlowState(
        nodes=nodes,
        edges=edges,
        selected_id=selected_id,
        timestamp=int(time.time() * 1000),
    )


def _reposition_workflow(pipeline: dict[str, Any], workflow: dict[str, Any]) -> dict[str, Any]:
    """Place nodes by execution stage without changing the user's edges."""

    rows: dict[str, int] = {}
    positions: dict[str, dict[str, float]] = {}
    for block in pipeline.get("blocks", []):
        stage = _semantic_stage(block, str(block.get("type", "")))
        row = rows.get(stage, 0)
        rows[stage] = row + 1
        stage_index = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else 2
        positions[str(block["id"])] = {"x": 65 + stage_index * 255, "y": 55 + row * 126}
    result = copy.deepcopy(workflow)
    result["nodes"] = [{"id": block_id, "position": position} for block_id, position in positions.items()]
    return result


def _render_module_library(
    pipeline: dict[str, Any], catalog: dict[str, dict[str, Any]], state: Any
) -> None:
    st.markdown('<div class="workflow-panel-title">模块库</div>', unsafe_allow_html=True)
    keyword = st.text_input("搜索模块", placeholder="例如：动量、RSI、风险", label_visibility="collapsed", key="workflow-module-search").strip().lower()
    st.caption("点击模块加入工作站，再从节点端口拖线连接。")
    for stage in STAGE_ORDER:
        choices = []
        for type_id, meta in catalog.items():
            if _semantic_stage(meta, type_id) != stage:
                continue
            haystack = f"{type_id} {meta.get('name', '')} {meta.get('description', '')}".lower()
            if keyword and keyword not in haystack:
                continue
            choices.append((type_id, meta))
        if not choices:
            continue
        with st.expander(STAGE_META[stage][0], expanded=bool(keyword)):
            for type_id, meta in choices:
                unavailable = bool(meta.get("disabled_reason")) or meta.get("available", True) is False
                label = f"＋ {meta.get('name', type_id)}"
                if st.button(label, key=f"workflow-add-{type_id}", use_container_width=True, help=str(meta.get("description", ""))):
                    block = _new_block(type_id, meta)
                    _insert_block(pipeline, block)
                    workflow = _state_to_workflow(state)
                    workflow["nodes"].append({"id": block["id"], "position": {"x": 80, "y": 80}})
                    pipeline["workflow"] = _reposition_workflow(pipeline, workflow)
                    st.session_state[STATE_PIPELINE] = pipeline
                    st.session_state[STATE_WORKFLOW] = _flow_state_from_pipeline(
                        pipeline, catalog, pipeline["workflow"], selected_id=block["id"]
                    )
                    st.session_state[STATE_NOTICE] = f"已添加：{meta.get('name', type_id)}；请为它连接输入和输出。"
                    st.rerun()
                if unavailable:
                    st.caption("数据未就绪 · 可放入画布查看，但不能启用回测")


def _render_node_inspector(
    pipeline: dict[str, Any], catalog: dict[str, dict[str, Any]], state: Any
) -> bool:
    """Render the selected node/edge.  Return True when graph structure changed."""

    st.markdown('<div class="workflow-panel-title">配置面板</div>', unsafe_allow_html=True)
    selected_id = str(state.selected_id or "")
    selected_edge = next((edge for edge in state.edges if str(edge.id) == selected_id), None)
    if selected_edge is not None:
        source = next((block for block in pipeline["blocks"] if str(block["id"]) == str(selected_edge.source)), None)
        target = next((block for block in pipeline["blocks"] if str(block["id"]) == str(selected_edge.target)), None)
        st.markdown(
            f'<div class="workflow-edge"><b>已选中连线</b><br>{(source or {}).get("name", selected_edge.source)} '
            f'→ {(target or {}).get("name", selected_edge.target)}</div>',
            unsafe_allow_html=True,
        )
        st.caption("连线代表上游输出会先于下游执行。右键连线也可以删除。")
        if st.button("删除这条连线", type="primary", use_container_width=True, key=f"workflow-delete-edge-{selected_id}"):
            state.edges = [edge for edge in state.edges if str(edge.id) != selected_id]
            state.selected_id = None
            return True
        return False

    block = next((item for item in pipeline["blocks"] if str(item["id"]) == selected_id), None)
    if block is None:
        st.info("点击画布中的模块或连线，在这里编辑。")
        st.markdown("**连接方法**")
        st.caption("从节点右侧圆点按住拖动，到下游节点左侧圆点松开。支持一对多、多对一和跨模块调用；不允许环路。")
        return False
    type_id = str(block.get("type", "unknown"))
    meta = catalog.get(type_id, {})
    stage = _semantic_stage(block, type_id)
    st.markdown(f"**{block.get('name') or meta.get('name') or type_id}**")
    st.code(type_id, language=None)
    st.caption(str(meta.get("description") or "该模块由后端注册表定义。"))
    status_label, status_text, css_class = _status(meta, type_id)
    st.markdown(f'<span class="{css_class}">{status_label}</span>', unsafe_allow_html=True)
    st.caption(status_text)
    unavailable = status_label == "数据未就绪"
    if unavailable:
        block["enabled"] = False
    previous_enabled = bool(block.get("enabled", True))
    block["enabled"] = st.toggle(
        "启用模块", value=bool(block.get("enabled", True)), disabled=unavailable,
        key=f"workflow-enabled-{block['id']}", help="停用后保留节点和参数，但后端不会调用。",
    )
    visual_changed = previous_enabled != bool(block["enabled"])
    if stage in {"filter", "strategy", "fundamental", "factor", "penalty"}:
        combine = str(block.get("combine", "AND")).upper()
        block["combine"] = st.selectbox(
            "多输入合并逻辑", ["AND", "OR"], index=0 if combine != "OR" else 1,
            key=f"workflow-combine-{block['id']}",
            help="AND 表示同时满足；OR 表示满足任一。后端仍会校验同组规则。",
        )
    st.markdown("**模块参数**")
    _render_block_params(block, meta, stage)
    st.markdown("---")
    if st.button("删除这个模块", use_container_width=True, key=f"workflow-delete-node-{block['id']}"):
        pipeline["blocks"] = [item for item in pipeline["blocks"] if str(item["id"]) != str(block["id"])]
        state.nodes = [node for node in state.nodes if str(node.id) != str(block["id"])]
        state.edges = [edge for edge in state.edges if str(edge.source) != str(block["id"]) and str(edge.target) != str(block["id"])]
        state.selected_id = None
        return True
    return visual_changed


def _render_workstation(
    pipeline: dict[str, Any], catalog: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], str | None]:
    """Render a Coze-like node workbench and compile its actual data graph."""

    try:
        from streamlit_flow import streamlit_flow
        from streamlit_flow.layouts import ManualLayout
        from backtest_lab.lego_workflow import compile_workflow, default_workflow
    except ImportError as exc:
        st.error(f"节点工作站组件未安装：{exc}")
        return pipeline, "节点工作站组件未安装"

    owner = f"{pipeline.get('id')}:{len(pipeline.get('blocks', []))}"
    if STATE_WORKFLOW not in st.session_state or st.session_state.get(STATE_WORKFLOW_OWNER) != owner:
        st.session_state[STATE_WORKFLOW] = _flow_state_from_pipeline(pipeline, catalog)
        st.session_state[STATE_WORKFLOW_OWNER] = owner
    state = st.session_state[STATE_WORKFLOW]

    toolbar = st.columns([1, 1, 1, 2.6])
    if toolbar[0].button("自动排版", use_container_width=True, help="按股票池→筛选→因子→成交从左到右整理节点，不改连线。"):
        pipeline["workflow"] = _reposition_workflow(pipeline, _state_to_workflow(state))
        state = _flow_state_from_pipeline(pipeline, catalog, pipeline["workflow"], state.selected_id)
        st.session_state[STATE_WORKFLOW] = state
        st.rerun()
    if toolbar[1].button("标准串联", use_container_width=True, help="按当前模块顺序重建一条标准数据链，会替换已有连线。"):
        workflow = _reposition_workflow(pipeline, default_workflow(pipeline["blocks"]))
        pipeline["workflow"] = workflow
        st.session_state[STATE_WORKFLOW] = _flow_state_from_pipeline(pipeline, catalog, workflow)
        st.rerun()
    if toolbar[2].button("清空连线", use_container_width=True, help="保留节点和参数，只清除连线。"):
        state.edges = []
        state.selected_id = None
        pipeline["workflow"] = _state_to_workflow(state)
        st.session_state[STATE_WORKFLOW] = _flow_state_from_pipeline(pipeline, catalog, pipeline["workflow"])
        st.rerun()
    toolbar[3].markdown(
        '<div class="workflow-help"><b>连线即执行关系</b>：从节点右侧端口拖到下游左侧端口；点击节点改参数，点击连线可删除。支持分支和汇合。</div>',
        unsafe_allow_html=True,
    )

    library_col, canvas_col, inspector_col = st.columns([0.82, 2.75, 1.05], gap="medium")
    with library_col:
        with st.container(height=700, border=True):
            _render_module_library(pipeline, catalog, state)
    with canvas_col:
        returned = streamlit_flow(
            "lego-coze-workstation",
            state,
            height=700,
            fit_view=False,
            show_controls=True,
            show_minimap=True,
            allow_new_edges=True,
            animate_new_edges=True,
            style={"backgroundColor": "#081116", "border": "1px solid #2b414d"},
            layout=ManualLayout(),
            get_node_on_click=True,
            get_edge_on_click=True,
            pan_on_drag=True,
            allow_zoom=True,
            min_zoom=0.25,
            enable_edge_menu=True,
            hide_watermark=True,
        )
        state = returned
        # Newly drawn React Flow edges are made deletable on the next render.
        for edge in state.edges:
            edge.deletable = True
            edge.focusable = True
            if not edge.label:
                edge.label = "数据流"
    changed = False
    with inspector_col:
        with st.container(height=700, border=True):
            changed = _render_node_inspector(pipeline, catalog, state)
    if changed:
        pipeline["workflow"] = _state_to_workflow(state)
        st.session_state[STATE_PIPELINE] = pipeline
        st.session_state[STATE_WORKFLOW] = _flow_state_from_pipeline(
            pipeline, catalog, pipeline["workflow"], state.selected_id
        )
        st.session_state[STATE_WORKFLOW_OWNER] = f"{pipeline.get('id')}:{len(pipeline.get('blocks', []))}"
        st.rerun()

    pipeline["workflow"] = _state_to_workflow(state)
    st.session_state[STATE_WORKFLOW] = state
    st.session_state[STATE_WORKFLOW_OWNER] = f"{pipeline.get('id')}:{len(pipeline.get('blocks', []))}"
    try:
        ordered, normalized = compile_workflow(pipeline["blocks"], pipeline["workflow"])
        pipeline["blocks"] = ordered
        pipeline["workflow"] = normalized
        return pipeline, None
    except Exception as exc:
        return pipeline, str(exc)


def render_lego_page(db: dict[str, Any], render_result: Callable[[dict[str, Any]], None]) -> None:
    """Render the complete Lego strategy builder page.

    Parameters
    ----------
    db:
        The existing ``database_summary()`` dictionary.  It is only used for
        truthful date boundaries and coverage labels.
    render_result:
        The host application's normal result renderer.  Keeping it as a
        callback avoids importing ``app.py`` and creating a circular import.
    """

    from backtest_lab.lego_pipeline import (  # Imported lazily for clean app startup.
        BLOCK_CATALOG,
        default_pipeline,
        list_pipelines,
        load_pipeline,
        run_lego_backtest,
        save_pipeline,
        validate_pipeline,
    )

    _render_styles()
    catalog = _catalog_dict(BLOCK_CATALOG)
    if STATE_PIPELINE not in st.session_state:
        st.session_state[STATE_PIPELINE] = _ensure_pipeline(default_pipeline())
    pipeline = _ensure_pipeline(st.session_state[STATE_PIPELINE])
    st.session_state[STATE_PIPELINE] = pipeline

    st.title("节点策略工作站")
    st.markdown(
        '<div class="lego-intro"><b>像扣子工作流一样搭策略：模块、端口、连线、分支和汇合都在一个工作站中完成。</b>'
        '<span>连线不是装饰：画布会编译成后端真实执行顺序。你可以自由调用模块、拖动排版、连接数据流并点击节点修改参数；环路、孤立节点和逆向连接不能运行。仅做模拟与历史回测。</span></div>',
        unsafe_allow_html=True,
    )
    notice = st.session_state.pop(STATE_NOTICE, None)
    if notice:
        st.success(str(notice))

    with st.expander("工作站设置：名称、市场、日期和资金", expanded=False):
        _render_baseplate(pipeline, db)
    pipeline, graph_error = _render_workstation(pipeline, catalog)
    st.session_state[STATE_PIPELINE] = pipeline

    # Validate after all block widgets have written their current values.  This
    # makes the message and Run button reflect the visible configuration rather
    # than the values from the preceding Streamlit rerun.
    validated, validation_error = (None, graph_error) if graph_error else _validate_for_ui(pipeline, validate_pipeline)
    if validation_error:
        st.warning(f"当前工作流尚不能运行：{validation_error}")
        st.caption("继续连接端口或调整模块；所有启用节点都必须从股票池入口连通到成交出口。")
    else:
        st.success("工作流校验通过：节点连线已编译为真实后端执行顺序。数据覆盖仍会在运行时逐项检查。")

    st.markdown("---")
    run_col, reset_col = st.columns([3, 1])
    run_disabled = validation_error is not None
    if run_col.button("▶ 校验并运行整套乐高回测", type="primary", disabled=run_disabled, use_container_width=True, help="逐日全市场筛选并排名，只把最终候选交给 A 股事件撮合层。"):
        try:
            with st.spinner("正在按积木顺序执行全市场筛选、打分、排名和 A 股撮合…"):
                result = run_lego_backtest(validated, persist=True)
            st.session_state[STATE_PIPELINE] = validated
            st.session_state[STATE_RESULT] = result
            st.success(f"回测完成：{result.get('run_id', '本次运行')}")
        except Exception as exc:
            st.error(f"回测未执行：{exc}")
    if reset_col.button("恢复默认积木", use_container_width=True, help="替换当前画布，不会删除已经保存的流水线。"):
        _clear_widget_state()
        st.session_state[STATE_PIPELINE] = _ensure_pipeline(default_pipeline())
        st.session_state.pop(STATE_WORKFLOW, None)
        st.session_state.pop(STATE_WORKFLOW_OWNER, None)
        st.session_state.pop(STATE_RESULT, None)
        st.rerun()

    result = st.session_state.get(STATE_RESULT)
    if result:
        _render_funnel_summary(result, pipeline)
        _render_rank_details(result)
        render_result(result)

    _render_save_load(pipeline, validate_pipeline, save_pipeline, list_pipelines, load_pipeline)


__all__ = ["render_lego_page"]
