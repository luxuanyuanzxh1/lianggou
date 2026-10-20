"""Composable, point-in-time-safe full-market research pipeline.

The module deliberately treats every decision as a block: universe, hard
filters, strategy filters, individual factors, risk penalties, ranking,
portfolio construction and execution.  Daily features and cross-sectional
ranking run in DuckDB; only the small ranked candidate set is sent to the
existing A-share execution simulator in :mod:`backtest_lab.full_market`.

Only historical simulation is implemented here.  There is no live broker
gateway and the only enabled execution block freezes a signal after the close
and trades at the following complete market day's open.
"""

from __future__ import annotations

import ast
import copy
import json
import hashlib
import math
import os
import re
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import duckdb
import numpy as np
import pandas as pd

from .config import MARKET_DB, PRODUCT_WORK_DIR, ROOT
from .engine import BacktestConfig, persist_result
from .full_market import run_full_market_backtest


PIPELINES_DIR = PRODUCT_WORK_DIR / "pipelines"
PIPELINE_VERSIONS_DIR = PRODUCT_WORK_DIR / "pipeline-versions"
CUSTOM_BLOCKS_FILE = PRODUCT_WORK_DIR / "custom-blocks.json"
ACTIVE_VALUE_FILE = ROOT / "public" / "data" / "active-market-value.json"
THS_ACTIVE_PROXY_FILE = ROOT / "public" / "data" / "active-market-value-ths-proxy.json"
ADJUSTED_KLINE_CACHE_DB = MARKET_DB.with_name("adjusted_kline_cache.duckdb")
LEGACY_EOD_CACHE_DB = MARKET_DB.with_name("a_share_eod.duckdb")
ACTIVE_MARKET_GATE_BLOCK_TYPES = {
    "filter_active_market_gate",
    "filter_ths_active_market_proxy_gate",
    "strategy_active_market_gate",
}
Z_ENTRY_BLOCK_TYPES = {
    "strategy_zettaranc_b1",
    "strategy_z_b123",
    "strategy_z_b1_stage",
    "strategy_z_b2_stage",
    "strategy_z_b3_stage",
}
Z_STANDARD_STAGE_BLOCK_TYPES = {
    "strategy_z_b123",
    "strategy_z_b1_stage",
    "strategy_z_b2_stage",
    "strategy_z_b3_stage",
}
STAGE_ORDER = {
    "universe": 0,
    "filter": 1,
    "strategy": 2,
    "factor": 3,
    "normalize": 4,
    "penalty": 5,
    "rank": 6,
    "portfolio": 7,
    "execution": 8,
}
ENABLED_UNIVERSE_SCOPES = {
    "all", "sh_sz", "sh", "sz", "bj", "main", "star", "chinext", "etf", "custom",
}


def _entry(
    category: str,
    name: str,
    description: str,
    params: dict[str, Any],
    *,
    available: bool = True,
    disabled_reason: str = "",
    required_data: Iterable[str] = (),
    **metadata: Any,
) -> dict[str, Any]:
    return {
        "category": category,
        "name": name,
        "description": description,
        "params": params,
        "available": available,
        "disabled_reason": disabled_reason,
        "required_data": list(required_data),
        **metadata,
    }


# Public registry.  Unavailable blocks stay visible so the UI can explain what
# data is missing instead of silently substituting a different data source.
BLOCK_CATALOG: dict[str, dict[str, Any]] = {
    "universe": _entry(
        "universe", "沪深京全市场", "上海、深圳、北京交易所全部 A 股。",
        {"scope": "all", "codes": []},
    ),
    "universe_sh_sz": _entry(
        "universe", "沪深 A 股", "上海和深圳交易所全部 A 股，不含北交所。",
        {"scope": "sh_sz", "codes": []},
    ),
    "universe_sh": _entry(
        "universe", "上证 A 股", "上海证券交易所全部 A 股。",
        {"scope": "sh", "codes": []},
    ),
    "universe_sz": _entry(
        "universe", "深证 A 股", "深圳证券交易所全部 A 股。",
        {"scope": "sz", "codes": []},
    ),
    "universe_bj": _entry(
        "universe", "北交所", "北京证券交易所股票。",
        {"scope": "bj", "codes": []},
    ),
    "universe_main": _entry(
        "universe", "沪深主板", "上海主板和深圳主板股票。",
        {"scope": "main", "codes": []},
    ),
    "universe_star": _entry(
        "universe", "科创板", "上海证券交易所科创板股票。",
        {"scope": "star", "codes": []},
    ),
    "universe_chinext": _entry(
        "universe", "创业板", "深圳证券交易所创业板股票。",
        {"scope": "chinext", "codes": []},
    ),
    "universe_etf": _entry(
        "universe", "ETF", "本地数据库中上海和深圳交易所的 ETF。",
        {"scope": "etf", "codes": []},
    ),
    "universe_custom": _entry(
        "universe", "自定义代码池", "输入六位股票或 ETF 代码建立自己的数据源。",
        {"scope": "custom", "codes": []},
    ),
    "universe_sse50": _entry(
        "universe", "上证50成分", "按历史日期使用上证50成分股。", {"scope": "sse50", "codes": []},
        available=False, disabled_reason="本地库缺少上证50历史成分变更表，禁止用当前成分回填历史。",
        required_data=("index_membership_pit:sse50",),
    ),
    "universe_csi300": _entry(
        "universe", "沪深300成分", "按历史日期使用沪深300成分股。", {"scope": "csi300", "codes": []},
        available=False, disabled_reason="本地库缺少沪深300历史成分变更表，禁止用当前成分回填历史。",
        required_data=("index_membership_pit:csi300",),
    ),
    "universe_csi500": _entry(
        "universe", "中证500成分", "按历史日期使用中证500成分股。", {"scope": "csi500", "codes": []},
        available=False, disabled_reason="本地库缺少中证500历史成分变更表，禁止用当前成分回填历史。",
        required_data=("index_membership_pit:csi500",),
    ),
    "universe_csi1000": _entry(
        "universe", "中证1000成分", "按历史日期使用中证1000成分股。", {"scope": "csi1000", "codes": []},
        available=False, disabled_reason="本地库缺少中证1000历史成分变更表，禁止用当前成分回填历史。",
        required_data=("index_membership_pit:csi1000",),
    ),
    "universe_csi_a500": _entry(
        "universe", "中证A500成分", "按历史日期使用中证A500成分股。", {"scope": "csi_a500", "codes": []},
        available=False, disabled_reason="本地库缺少中证A500历史成分变更表，禁止用当前成分回填历史。",
        required_data=("index_membership_pit:csi_a500",),
    ),
    "filter_non_st": _entry(
        "filter", "剔除 ST", "按本地证券主表名称剔除当前 ST；历史 ST 状态尚非 PIT。",
        {"group": "hard_non_st"},
    ),
    "filter_tradable": _entry(
        "filter", "可交易", "信号日成交量、成交额和 OHLC 必须有效。",
        {"group": "hard_tradable"},
    ),
    "filter_not_limit_up": _entry(
        "filter", "剔除收盘涨停", "排除信号日收盘已触及对应板块涨停价的股票。",
        {"group": "hard_limit"},
    ),
    "filter_listing_days": _entry(
        "filter", "上市天数", "按数据库首根日线计算上市自然日。",
        {"group": "hard_listing", "min_days": 120},
    ),
    "filter_adv20": _entry(
        "filter", "成交额门槛", "过去 N 日平均成交额必须达到下限。",
        {"group": "hard_liquidity", "window": 20, "min_amount": 20_000_000.0},
    ),
    "filter_custom_formula": _entry(
        "filter", "自定义筛选条件", "使用安全公式决定股票是否通过这一层硬筛。",
        {"group": "custom_filter", "name": "我的筛选", "formula": "close > MA(20)"},
    ),
    "filter_active_market_gate": _entry(
        "filter", "活跃市值", "使用指南针活跃市值原值：单日涨幅达到4%后开启；单日跌幅≤-2.33%或跌破M日均线，任一条件触发即关闭。",
        {"group": "active_market_gate", "provider": "compass", "start_one": 4.0, "use_two_day": False,
         "start_two": 5.0, "exit_one": -2.33, "ma_period": 10, "max_gap_days": 0},
        required_data=("public/data/active-market-value.json:candles",),
    ),
    "filter_ths_active_market_proxy_gate": _entry(
        "filter", "同花顺活跃市值拟合门控",
        "六指数、沪深全A宽度与成交额拟合的独立代理；不是指南针原值，仅用于回测。",
        {"group": "active_market_proxy_gate", "provider": "ths_internal_proxy",
         "start_one": 3.75, "use_two_day": True, "start_two": 4.95,
         "exit_one": -2.05, "ma_period": 10, "ma_buffer": 0.0025,
         "max_gap_days": 0},
        required_data=("public/data/active-market-value-ths-proxy.json:candles",),
    ),
    "strategy_momentum": _entry(
        "strategy", "动量初选", "当前收盘相对 N 日前收盘的收益达到阈值。",
        {"group": "strategy", "lookback": 20, "min_return": 0.0},
    ),
    "strategy_ma_cross": _entry(
        "strategy", "双均线初选", "短期均线位于长期均线上方。",
        {"group": "strategy", "fast": 5, "slow": 20},
    ),
    "strategy_rsi": _entry(
        "strategy", "RSI 初选", "RSI 位于指定区间；数值只使用当日及此前数据。",
        {"group": "strategy", "period": 14, "min_value": 0.0, "max_value": 70.0},
    ),
    "strategy_turtle": _entry(
        "strategy", "海龟突破初选", "收盘价突破此前 N 日最高价（不含当日）。",
        {"group": "strategy", "entry": 20},
    ),
    "strategy_zettaranc_b1": _entry(
        "strategy", "Z·严格 B1", "同花顺活跃市值门控后的日线硬筛：J≤-10、缩量窄幅、N型上移、非绿砖、双线多头与MACD零轴。",
        {"group": "zettaranc_b1", "j_threshold": -10.0,
         "min_gain": -2.0, "max_gain": 1.8,
         "main_max_amplitude": 4.0, "growth_max_amplitude": 7.0,
         "max_volume_ratio": 0.8, "n_lift_floor": 0.98,
         "require_non_green_brick": True, "require_white_above_yellow": True,
         "require_support": True, "require_macd_positive": True,
         "require_no_s1": True},
        required_data=("daily_bar:OHLCV", "filter_ths_active_market_proxy_gate"),
        rule_version="ruipan-z-v1.0",
        rule_status="standard",
        rule_source="zettaranc-skill v4.0.3（MIT规则提取版）",
        rule_source_url="https://github.com/lululu811/zettaranc-skill/tree/42165b65689f86b96fa302855d93864e847869ce",
        rule_items=(
            "同花顺活跃市值代理必须开启，评分不能绕过门控。",
            "J≤-10、缩量、窄振幅、N型低点上移且当前非绿砖。",
            "白线≥黄线、守住MA60或黄线，并由MACD DIF零轴否决开仓。",
            "T日正式收盘形成信号，T+1开盘执行。",
        ),
        rule_notes=("纯日线不冒充上游9:33/9:37及14:55盘中规则。",),
    ),
    "strategy_z_b123": _entry(
        "strategy", "Z·B1/B2/B3 三阶段准入",
        "固定标准日线序列：B1纪律低吸、其后1—3日B2放量确认、B2后1—2日B3缩量中继；任一阶段命中才进入因子排序。",
        {"group": "z_b123", "allow_b1": True, "allow_b2": True, "allow_b3": True},
        required_data=("daily_bar:OHLCV", "filter_ths_active_market_proxy_gate"),
        rule_version="ruipan-z-v1.0",
        rule_status="standard",
        rule_source="zettaranc-skill v4.0.3（MIT规则提取版）",
        rule_source_url="https://github.com/lululu811/zettaranc-skill/tree/42165b65689f86b96fa302855d93864e847869ce",
        rule_items=(
            "B1完整满足J≤-10、涨跌幅、分板振幅、量比≤0.8、N型、砖值、双线、支撑、MACD与S1否决。",
            "B2只承认此前1—3个交易日真实标准B1；当天涨幅≥4%、量比≥1.2、J<55且向上。",
            "B3只承认此前1—2个交易日真实B2；当天涨幅(0,2%)、振幅<7%、量比≤0.7。",
            "同花顺代理门控是硬条件，高分不能补回不合格股票。",
        ),
        rule_notes=("B3仅代表正式日线收盘确认，不冒充盘中分歧转一致。",),
    ),
    "strategy_z_b1_stage": _entry(
        "strategy", "Z·B1 独立低吸策略",
        "只交易完整标准B1：超卖、缩量、窄幅、N型、非绿砖、双线与MACD纪律全部成立。",
        {"group": "z_b1_stage"},
        required_data=("daily_bar:OHLCV", "filter_ths_active_market_proxy_gate"),
        rule_version="ruipan-z-v1.0", rule_status="standard",
        rule_source="zettaranc-skill v4.0.3（MIT规则提取版）",
        rule_source_url="https://github.com/lululu811/zettaranc-skill/tree/42165b65689f86b96fa302855d93864e847869ce",
        rule_items=("仅接受标准B1。", "T日收盘确认，T+1开盘执行。", "活跃市值门控不可绕过。"),
    ),
    "strategy_z_b2_stage": _entry(
        "strategy", "Z·B2 独立确认策略",
        "只交易标准B2：此前1—3日必须存在真实B1，当日放量上涨且J值向上确认。",
        {"group": "z_b2_stage"},
        required_data=("daily_bar:OHLCV", "filter_ths_active_market_proxy_gate"),
        rule_version="ruipan-z-v1.0", rule_status="standard",
        rule_source="zettaranc-skill v4.0.3（MIT规则提取版）",
        rule_source_url="https://github.com/lululu811/zettaranc-skill/tree/42165b65689f86b96fa302855d93864e847869ce",
        rule_items=("此前1—3日必须有标准B1。", "当日涨幅、量比和J值全部确认。", "活跃市值门控不可绕过。"),
    ),
    "strategy_z_b3_stage": _entry(
        "strategy", "Z·B3 独立中继策略",
        "只交易标准B3：此前1—2日必须存在真实B2，当日小阳、窄幅、缩量中继。",
        {"group": "z_b3_stage"},
        required_data=("daily_bar:OHLCV", "filter_ths_active_market_proxy_gate"),
        rule_version="ruipan-z-v1.0", rule_status="standard",
        rule_source="zettaranc-skill v4.0.3（MIT规则提取版）",
        rule_source_url="https://github.com/lululu811/zettaranc-skill/tree/42165b65689f86b96fa302855d93864e847869ce",
        rule_items=("此前1—2日必须有标准B2。", "当日涨幅(0,2%)、振幅<7%、量比≤0.7。", "活跃市值门控不可绕过。"),
    ),
    "strategy_z_exit_risk_guard": _entry(
        "strategy", "Z·退出风险剔除（不开新仓）",
        "在候选层剔除S1、红砖翻绿、白线死叉黄线或跌破黄线；仅用于阻止新仓和风险审计，不冒充T+1卖出成交。",
        {"group": "z_exit_risk", "forbid_s1": True, "forbid_green_flip": True,
         "forbid_white_dead_cross": True, "forbid_below_yellow": True},
        required_data=("任一Z·B1/B2/B3准入策略",),
        rule_version="ruipan-z-v1.0",
        rule_status="standard",
        rule_source="睿盘确定性退出风控",
        rule_source_url="https://github.com/lululu811/zettaranc-skill/tree/42165b65689f86b96fa302855d93864e847869ce",
        rule_items=(
            "S1、红砖翻绿、白线死叉黄线、跌破黄线任一命中即拒绝成为新候选。",
            "活跃市值关门由执行器在T+1开盘强退并逐日重试。",
            "个股技术退出在本乐高V1只做候选剔除/研究标注；不能显示成已经卖出。",
        ),
        rule_notes=("明确止损和时间止损依赖持仓成本/持有日数，应由有状态策略或执行器处理。",),
    ),
    "strategy_active_market_gate": _entry(
        "strategy", "活跃市值市场门控", "使用活跃市值指数决定策略是否开启；跌幅≤-2.33%或跌破M日均线任一触发即关闭。",
        {"group": "market_gate", "provider": "compass", "start_one": 4.0, "use_two_day": False, "start_two": 5.0,
         "exit_one": -2.33, "ma_period": 10, "max_gap_days": 0},
        required_data=("public/data/active-market-value.json:candles",),
    ),
    "saved_strategy": _entry(
        "strategy", "本地已保存策略", "选择已登记的本地策略包并传入参数。",
        {"group": "strategy", "strategy_id": "", "params": {}},
        available=False,
        disabled_reason="V1 不执行未经白名单审核的任意 Python 策略；接口已预留。",
        required_data=("strategy_registry", "sandboxed_strategy_compiler"),
    ),
    "custom_formula": _entry(
        "strategy", "自定义策略条件", "使用安全公式定义自己的初选或市场条件。",
        {"group": "custom_strategy", "name": "我的策略条件", "formula": "MOM(20) > 0"},
    ),
    "factor_momentum": _entry(
        "factor", "动量因子", "一项因子一块，可独立设权重、方向和标准化方式。",
        {"lookback": 20, "weight": 0.30, "direction": "higher", "method": "robust_sigmoid"},
    ),
    "factor_trend": _entry(
        "factor", "趋势因子", "短均线相对长均线的强度。",
        {"fast": 5, "slow": 20, "weight": 0.20, "direction": "higher", "method": "robust_sigmoid"},
    ),
    "factor_rsi": _entry(
        "factor", "RSI 因子", "RSI 横截面得分。方向可配置为强势优先或超卖优先。",
        {"period": 14, "weight": 0.10, "direction": "higher", "method": "percentile"},
    ),
    "factor_low_volatility": _entry(
        "factor", "低波动因子", "N 日收益波动率，默认越低越好。",
        {"window": 20, "weight": 0.15, "direction": "lower", "method": "robust_sigmoid"},
    ),
    "factor_liquidity": _entry(
        "factor", "流动性因子", "N 日平均成交额，默认越高越好。",
        {"window": 20, "weight": 0.10, "direction": "higher", "method": "robust_sigmoid"},
    ),
    "factor_drawdown": _entry(
        "factor", "回撤因子", "相对 N 日最高收盘的回撤，默认越接近零越好。",
        {"window": 60, "weight": 0.15, "direction": "higher", "method": "robust_sigmoid"},
    ),
    "factor_zettaranc_quality": _entry(
        "factor", "Z·结构质量", "只对已通过Z·硬筛的股票排序：综合J值深度、缩量程度、双线强度与支撑距离。",
        {"weight": 0.30, "direction": "higher", "method": "percentile"},
        required_data=("strategy_zettaranc_b1",),
        rule_version="ruipan-z-v1.0",
        rule_status="standard",
        rule_source="zettaranc-skill v4.0.3（MIT规则提取版）",
        rule_source_url="https://github.com/lululu811/zettaranc-skill/tree/42165b65689f86b96fa302855d93864e847869ce",
        rule_items=(
            "仅在已通过Z·B1或B1/B2/B3三阶段硬筛的股票中排序。",
            "J值深度、缩量、双线强度和支撑距离只影响名次。",
            "高分不能让无Z·准入信号股票进入组合。",
        ),
    ),
    "factor_z_resonance": _entry(
        "factor", "Z·阶段共振",
        "只在三阶段硬准入之后排序：B3、B2、B1依次获得阶段分，再叠加双线、MACD和砖值共振；绝不替代硬筛。",
        {"weight": 0.30, "direction": "higher", "method": "percentile"},
        required_data=("strategy_z_b123",),
        rule_version="ruipan-z-v1.0",
        rule_status="standard",
        rule_source="zettaranc-skill v4.0.3（MIT规则提取版）",
        rule_source_url="https://github.com/lululu811/zettaranc-skill/tree/42165b65689f86b96fa302855d93864e847869ce",
        rule_items=(
            "阶段权重只改变已合格股票的名次：B3>B2>B1。",
            "白黄线强度、MACD DIF和非绿砖只作共振加分。",
            "没有真实B1/B2/B3时，共振分不能产生候选。",
        ),
    ),
    "factor_custom_formula": _entry(
        "factor", "自定义因子", "使用白名单行情字段和滚动函数创建自己的横截面评分因子。",
        {"name": "我的因子", "formula": "MOM(20)", "weight": 0.10,
         "direction": "higher", "method": "robust_sigmoid"},
    ),
    "factor_industry_concept_trend": _entry(
        "factor", "行业/概念趋势加分", "股票所属行业或概念位于知行线和多空线上方时加分。",
        {"weight": 0.10, "direction": "higher", "method": "percentile"},
        available=False,
        disabled_reason="板块成分关系只有当前快照，缺少历史 PIT 成分和知行/多空线口径，禁止未来信息泄漏。",
        required_data=("sector_membership_pit", "sector_daily_adjusted", "zhixing_formula"),
    ),
    "factor_cyc": _entry(
        "factor", "CYC 成本均线", "CYC 主图成本均线派生因子。",
        {"period": 13, "weight": 0.10, "direction": "higher", "method": "robust_sigmoid"},
        available=False,
        disabled_reason="本地日线缺少历史自由流通盘/换手率，不能可靠复现 CYC。",
        required_data=("free_float_shares_pit", "turnover_rate"),
    ),
    "factor_brick": _entry(
        "factor", "Z·砖形状态因子", "复用已审计的Z课程日线砖形递推：红砖优先、绿砖靠后，并用连续砖值细分同色股票。",
        {"weight": 0.10, "direction": "higher", "method": "percentile"},
        required_data=("daily_bar:OHLCV", "zettaranc_brick_audit"),
        rule_version="ruipan-z-v1.0",
        rule_status="audited",
        rule_source="zettaranc-skill v4.0.3（MIT规则提取版）",
        rule_source_url="https://github.com/lululu811/zettaranc-skill/tree/42165b65689f86b96fa302855d93864e847869ce",
        rule_items=(
            "砖形递推只使用当日及此前复权日线，不读取未来行情。",
            "红砖状态记为正、绿砖状态记为负；连续砖值只用于同状态内排序。",
            "该节点是排序因子，不能代替Z·B1/B2/B3硬准入条件。",
        ),
    ),
    "factor_fundamental_ai": _entry(
        "factor", "AI 基本面评分", "AI 提取公告风险标签，确定性财务指标负责最终评分。",
        {"weight": 0.20, "direction": "higher", "method": "robust_sigmoid", "model": "disabled"},
        available=False,
        disabled_reason="缺少带公告发布日期的 PIT 财报库；禁止把最新财务数据用于历史日期。",
        required_data=("fundamentals_pit", "filing_publish_date", "ai_score_cache"),
    ),
    "factor_machine_learning": _entry(
        "factor", "机器学习/积极学习评分", "滚动训练模型输出每日横截面分数，并支持人工标注后的积极学习迭代。",
        {"weight": 0.20, "direction": "higher", "method": "percentile",
         "model_id": "", "train_window": 500, "retrain_every": 20},
        available=False,
        disabled_reason="需先建立按日期切分的特征仓、标签和滚动训练产物；当前禁止用全样本拟合后回填历史。",
        required_data=("pit_feature_store", "walk_forward_model_registry", "label_version"),
    ),
    "normalize_cross_section": _entry(
        "normalize", "横截面标准化", "统一控制稳健 S 形变换的斜率和截断；每个因子仍可选择 S 形或百分位。",
        {"sigmoid_k": 1.0, "clip": 6.0, "winsorize": "median_mad"},
    ),
    "penalty_volatility": _entry(
        "penalty", "波动惩罚", "波动越高扣分越多。",
        {"window": 20, "weight": 0.08, "method": "percentile"},
    ),
    "penalty_drawdown": _entry(
        "penalty", "回撤惩罚", "回撤绝对值越大扣分越多。",
        {"window": 60, "weight": 0.08, "method": "percentile"},
    ),
    "penalty_liquidity": _entry(
        "penalty", "低流动性惩罚", "平均成交额越低扣分越多。",
        {"window": 20, "weight": 0.05, "method": "percentile"},
    ),
    "penalty_custom_formula": _entry(
        "penalty", "自定义风险惩罚", "使用安全数值公式定义风险值；数值越高，扣分越多。",
        {"name": "我的风险惩罚", "formula": "VOL(20)", "weight": 0.05,
         "method": "percentile"},
    ),
    "penalty_delta_rp": _entry(
        "penalty", "Delta RP 惩罚", "为用户原始 Delta RP 公式预留的风险惩罚接口。",
        {"weight": 0.10, "formula_version": "pending"},
        available=False,
        disabled_reason="尚未取得明确 Delta RP 数学定义；为避免伪造结果，本块保持禁用。",
        required_data=("delta_rp_formula",),
    ),
    "rank_topk": _entry(
        "rank", "每日 Top K", "按最终得分降序输出每日候选和完整分项得分。",
        {"top_k": 50},
    ),
    "portfolio": _entry(
        "portfolio", "组合构建", "从每日排名中取健康持仓数，并按固定交易日间隔调仓。",
        {"positions": 10, "rebalance_every": 5, "weighting": "equal"},
    ),
    "execution_next_open": _entry(
        "execution", "次日开盘执行", "收盘后冻结信号，下一完整交易日开盘按 A 股规则模拟成交；候选不足保留现金。",
        {"mode": "next_open", "commission": 0.0003, "min_commission": 5.0,
         "transfer_fee": 0.00001, "stamp_tax": 0.0005, "slippage": 0.0005,
         "lot_size": 100, "invest_ratio": 0.95, "max_position_pct": 0.10,
         "max_participation": 0.10},
    ),
    "execution_1445": _entry(
        "execution", "14:45 执行", "按 14:45 分钟线产生和撮合信号。",
        {"mode": "same_day_1445", "time": "14:45"},
        available=False,
        disabled_reason="本地分钟库没有全市场、全区间 14:45 PIT 覆盖，严禁以日线价格静默替代。",
        required_data=("full_market_1445_minutes",),
    ),
}


# Every factor exposed in the node library is registered here and compiled by
# the same backend.  This keeps UI availability, required history and the SQL
# implementation in one place; a factor cannot become a decorative placeholder.
FACTOR_REGISTRY: dict[str, dict[str, Any]] = {
    "factor_momentum": {"name": "区间动量", "family": "动量", "description": "当前收盘相对 N 个交易日前收盘的收益。", "params": {"lookback": 20}, "formula": "close / close[-N] - 1"},
    "factor_multi_momentum": {"name": "多周期动量", "family": "动量", "description": "短、中、长三个周期动量的等权组合。", "params": {"short": 5, "medium": 20, "long": 60}, "formula": "mean(MOM(short), MOM(medium), MOM(long))"},
    "factor_short_reversal": {"name": "短期反转", "family": "动量", "description": "过去 N 日涨幅取反，衡量短期均值回归。", "params": {"lookback": 5}, "formula": "-MOM(N)", "direction": "higher"},
    "factor_trend": {"name": "双均线趋势", "family": "趋势/均线", "description": "短期均线相对长期均线的强度。", "params": {"fast": 5, "slow": 20}, "formula": "MA(fast) / MA(slow) - 1"},
    "factor_ma_position": {"name": "价格相对 MA", "family": "趋势/均线", "description": "收盘价相对 N 日均线的位置。", "params": {"period": 20}, "formula": "close / MA(N) - 1"},
    "factor_ma_cross_event": {"name": "上穿 MA 线", "family": "趋势/均线", "description": "今日收盘上穿 N 日均线时输出 1。", "params": {"period": 20}, "formula": "close>MA(N) and prev_close<=prev_MA(N)"},
    "factor_ma_breakdown_event": {"name": "跌破 MA 线", "family": "趋势/均线", "description": "今日收盘跌破 N 日均线时输出 1。", "params": {"period": 20}, "formula": "close<MA(N) and prev_close>=prev_MA(N)"},
    "factor_ma_deviation": {"name": "均线乖离率", "family": "趋势/均线", "description": "价格偏离 N 日均线的百分比。", "params": {"period": 20}, "formula": "100 * (close / MA(N) - 1)"},
    "factor_ma_slope": {"name": "均线斜率", "family": "趋势/均线", "description": "N 日均线相对 K 日前的变化率。", "params": {"period": 20, "slope_days": 5}, "formula": "MA(N) / MA(N)[-K] - 1"},
    "factor_ma_alignment": {"name": "均线多头排列", "family": "趋势/均线", "description": "短中长期均线排列强度。", "params": {"fast": 5, "medium": 10, "slow": 20}, "formula": "MA(fast)/MA(medium)+MA(medium)/MA(slow)-2"},
    "factor_ema_cross": {"name": "EMA 金叉状态", "family": "趋势/均线", "description": "短 EMA 相对长 EMA 的强度，参数使用真实递推 EMA。", "params": {"fast": 12, "slow": 26}, "formula": "EMA(fast) / EMA(slow) - 1"},
    "factor_trend_efficiency": {"name": "趋势效率", "family": "趋势/均线", "description": "净价格位移与逐日路径长度之比。", "params": {"window": 20}, "formula": "abs(close-close[-N]) / sum(abs(delta), N)"},
    "factor_donchian_position": {"name": "唐奇安通道位置", "family": "趋势/均线", "description": "收盘价在 N 日最高价和最低价之间的位置。", "params": {"window": 20}, "formula": "(close-LLV(low,N))/(HHV(high,N)-LLV(low,N))"},
    "factor_macd_dif": {"name": "MACD DIF", "family": "MACD", "description": "标准 EMA12 与 EMA26 之差。", "params": {}, "formula": "EMA(12)-EMA(26)"},
    "factor_macd_histogram": {"name": "MACD 柱", "family": "MACD", "description": "标准 MACD 柱值 2×(DIF-DEA)。", "params": {}, "formula": "2*(DIF-DEA)"},
    "factor_macd_cross": {"name": "MACD 金叉", "family": "MACD", "description": "DIF 今日上穿 DEA 时输出 1。", "params": {}, "formula": "DIF>DEA and prev_DIF<=prev_DEA"},
    "factor_rsi": {"name": "RSI 强弱", "family": "摆动指标", "description": "N 日 RSI 横截面强弱。", "params": {"period": 14}, "formula": "RSI(N)"},
    "factor_kdj_k": {"name": "KDJ K值", "family": "摆动指标", "description": "标准 KDJ(9,3,3) 的 K 值。", "params": {}, "formula": "KDJ.K"},
    "factor_kdj_j": {"name": "KDJ J值", "family": "摆动指标", "description": "标准 KDJ(9,3,3) 的 J 值。", "params": {}, "formula": "3*K-2*D"},
    "factor_cci": {"name": "CCI", "family": "摆动指标", "description": "典型价格相对 N 日平均绝对偏差的 CCI。", "params": {"period": 14}, "formula": "(TP-MA(TP,N))/(0.015*MAD(TP,N))"},
    "factor_williams_r": {"name": "Williams %R", "family": "摆动指标", "description": "收盘价在 N 日高低区间的位置，输出 -100 至 0。", "params": {"period": 14}, "formula": "-100*(HHV(high,N)-close)/(HHV(high,N)-LLV(low,N))"},
    "factor_high_52w_proximity": {"name": "接近 52 周新高", "family": "动量", "description": "收盘价相对 252 日最高收盘的距离。", "params": {"window": 252}, "formula": "close/HHV(close,N)-1"},
    "factor_low_volatility": {"name": "低波动", "family": "波动/风险", "description": "N 日收益率标准差，默认越低越好。", "params": {"window": 20}, "formula": "STD(return,N)", "direction": "lower"},
    "factor_atr": {"name": "ATR", "family": "波动/风险", "description": "N 日平均真实波幅。", "params": {"window": 14}, "formula": "MA(TR,N)", "direction": "lower"},
    "factor_natr": {"name": "归一化 ATR", "family": "波动/风险", "description": "ATR 相对收盘价的比例。", "params": {"window": 14}, "formula": "ATR(N)/close", "direction": "lower"},
    "factor_bollinger_position": {"name": "布林带位置", "family": "波动/风险", "description": "价格在 N 日均线±K倍标准差通道中的位置。", "params": {"window": 20, "stddev": 2.0}, "formula": "(close-lower)/(upper-lower)"},
    "factor_bollinger_width": {"name": "布林带宽度", "family": "波动/风险", "description": "布林带上下轨宽度相对中轨的比例。", "params": {"window": 20, "stddev": 2.0}, "formula": "(upper-lower)/middle", "direction": "lower"},
    "factor_amplitude": {"name": "日内振幅", "family": "波动/风险", "description": "当日最高最低价差相对昨收。", "params": {}, "formula": "(high-low)/prev_close", "direction": "lower"},
    "factor_downside_vol": {"name": "下行波动率", "family": "波动/风险", "description": "只统计负收益的 N 日下行波动。", "params": {"window": 20}, "formula": "sqrt(mean(min(return,0)^2,N))", "direction": "lower"},
    "factor_volatility_change": {"name": "波动率变化", "family": "波动/风险", "description": "短周期波动相对长周期波动的变化。", "params": {"short": 10, "long": 60}, "formula": "VOL(short)/VOL(long)-1", "direction": "lower"},
    "factor_drawdown": {"name": "区间回撤", "family": "波动/风险", "description": "收盘价相对 N 日最高收盘的回撤。", "params": {"window": 60}, "formula": "close/HHV(close,N)-1"},
    "factor_liquidity": {"name": "平均成交额", "family": "成交量/流动性", "description": "N 日平均成交额。", "params": {"window": 20}, "formula": "MA(amount,N)"},
    "factor_volume_ratio": {"name": "量比", "family": "成交量/流动性", "description": "当日成交量相对 N 日均量。", "params": {"window": 20}, "formula": "volume/MA(volume,N)"},
    "factor_amount_ratio": {"name": "成交额比", "family": "成交量/流动性", "description": "当日成交额相对 N 日平均成交额。", "params": {"window": 20}, "formula": "amount/MA(amount,N)"},
    "factor_volume_ma_cross": {"name": "成交量上穿均量", "family": "成交量/流动性", "description": "当日成交量上穿 N 日均量时输出 1。", "params": {"window": 20}, "formula": "volume>VMA(N) and prev_volume<=prev_VMA(N)"},
    "factor_obv_momentum": {"name": "OBV 动量", "family": "成交量/流动性", "description": "标准 OBV 相对 N 日前的变化，并按均量归一化。", "params": {"window": 20}, "formula": "(OBV-OBV[-N])/VMA(N)"},
    "factor_mfi": {"name": "MFI 资金流量", "family": "成交量/流动性", "description": "典型价格与成交量构成的 N 日资金流量指标。", "params": {"period": 14}, "formula": "MFI(N)"},
    "factor_cmf": {"name": "Chaikin Money Flow", "family": "成交量/流动性", "description": "N 日收盘位置加权成交量占比。", "params": {"window": 20}, "formula": "sum(CLV*volume,N)/sum(volume,N)"},
    "factor_ad_slope": {"name": "A/D 量价斜率", "family": "成交量/流动性", "description": "N 日收盘位置成交量的平均方向强度。", "params": {"window": 20}, "formula": "mean(CLV*volume,N)/VMA(N)"},
    "factor_price_volume_corr": {"name": "价量相关性", "family": "成交量/流动性", "description": "N 日收益率与成交量变化率的相关系数。", "params": {"window": 20}, "formula": "corr(return,volume_change,N)"},
    "factor_volume_contraction": {"name": "缩量程度", "family": "成交量/流动性", "description": "N 日均量相对长周期均量，越低代表缩量。", "params": {"short": 5, "long": 20}, "formula": "VMA(short)/VMA(long)-1", "direction": "lower"},
    "factor_amihud": {"name": "Amihud 非流动性", "family": "成交量/流动性", "description": "N 日绝对收益与成交额之比的均值。", "params": {"window": 20}, "formula": "mean(abs(return)/amount,N)", "direction": "lower"},
    "factor_amount_stability": {"name": "成交额稳定性", "family": "成交量/流动性", "description": "N 日成交额变异系数。", "params": {"window": 20}, "formula": "STD(amount,N)/MA(amount,N)", "direction": "lower"},
    "factor_liquidity_shock": {"name": "流动性冲击", "family": "成交量/流动性", "description": "当日成交额相对 N 日均值的异常程度。", "params": {"window": 20}, "formula": "amount/ADV(N)-1"},
    "factor_intraday_strength": {"name": "日内收盘位置", "family": "K线/另类代理", "description": "收盘价在当日最高最低区间中的位置。", "params": {}, "formula": "(close-low)/(high-low)"},
    "factor_overnight_return": {"name": "隔夜收益", "family": "K线/另类代理", "description": "当日开盘相对昨收收益。", "params": {}, "formula": "open/prev_close-1"},
    "factor_body_ratio": {"name": "实体占比", "family": "K线/另类代理", "description": "K线实体长度占当日振幅比例。", "params": {}, "formula": "abs(close-open)/(high-low)"},
    "factor_upper_shadow": {"name": "上影线占比", "family": "K线/另类代理", "description": "上影线长度占当日振幅比例。", "params": {}, "formula": "(high-max(open,close))/(high-low)", "direction": "lower"},
    "factor_lower_shadow": {"name": "下影线占比", "family": "K线/另类代理", "description": "下影线长度占当日振幅比例。", "params": {}, "formula": "(min(open,close)-low)/(high-low)"},
    "factor_range_compression": {"name": "振幅收缩", "family": "K线/另类代理", "description": "当日振幅相对 N 日平均振幅。", "params": {"window": 20}, "formula": "range/mean(range,N)", "direction": "lower"},
    "factor_attention_proxy": {"name": "市场关注度代理", "family": "K线/另类代理", "description": "量比与绝对收益的乘积，只是行情代理，不冒充新闻热度。", "params": {"window": 20}, "formula": "volume_ratio*abs(return)"},
    "factor_crowding_proxy": {"name": "交易拥挤度代理", "family": "K线/另类代理", "description": "动量强度、量比与波动率共同刻画的行情拥挤代理。", "params": {"window": 20}, "formula": "abs(MOM(N))*volume_ratio/VOL(N)"},
    "factor_limit_up_proximity": {"name": "接近涨停程度", "family": "K线/另类代理", "description": "当日涨幅占对应板块涨停幅度的比例。", "params": {}, "formula": "return/board_limit"},
    "factor_brick": {"name": "砖形趋势因子", "family": "K线/另类代理", "description": "按通达信砖形递推公式计算红绿砖趋势，不依赖任何 Z 策略。", "params": {}, "formula": "brick_state + 0.001*brick_value"},
}

_FACTOR_FAMILY_GUIDES: dict[str, dict[str, str]] = {
    "趋势/均线": {
        "best_for": "观察趋势方向、突破确认和持仓中的趋势延续。",
        "pitfall": "震荡市会频繁来回穿越，单独使用容易产生假突破。",
        "visual": "重点比较收盘价与均线的位置、斜率及交叉发生的日期。",
    },
    "MACD": {
        "best_for": "观察中短期趋势动能、零轴位置和金叉死叉。",
        "pitfall": "MACD 来自价格的二次平滑，确认较慢，不能把滞后信号当成提前预测。",
        "visual": "结合 K 线趋势查看 DIF、DEA 与柱体由弱转强或由强转弱。",
    },
    "动量": {
        "best_for": "比较股票在固定回看周期内的相对强弱与趋势持续性。",
        "pitfall": "极端高动量可能处于拥挤末端，短周期动量也可能快速反转。",
        "visual": "观察一段连续上涨或下跌 K 线后，因子值如何同步扩张或回落。",
    },
    "摆动指标": {
        "best_for": "识别超买、超卖和震荡区间中的强弱切换。",
        "pitfall": "强趋势中指标可能长期钝化，不能看到超买就机械卖出、看到超卖就机械买入。",
        "visual": "对照 K 线拐点，观察指标进入极端区域后是否出现价格确认。",
    },
    "波动/风险": {
        "best_for": "控制风险、识别波动扩张或收缩，并辅助设置仓位和止损。",
        "pitfall": "低波动不等于低风险，长期平静后也可能突然跳空或跌停。",
        "visual": "比较大实体、大振幅 K 线出现前后，波动指标是否明显上升。",
    },
    "成交量/流动性": {
        "best_for": "验证价格变化是否得到成交量和资金活跃度配合。",
        "pitfall": "放量既可能是突破确认，也可能是高位派发，必须结合价格位置判断。",
        "visual": "同时观察 K 线方向、成交量柱和因子值，判断量价是否同向。",
    },
    "K线/另类代理": {
        "best_for": "刻画单根 K 线结构、短期交易行为以及仅由行情构造的替代信号。",
        "pitfall": "代理因子不等于真实新闻热度、资金身份或投资者情绪，只能说明行情表象。",
        "visual": "在典型长影线、放量大阳/大阴或区间收缩位置观察因子变化。",
    },
}
_BINARY_FACTOR_TYPES = {
    "factor_ma_cross_event", "factor_ma_breakdown_event", "factor_macd_cross",
    "factor_volume_ma_cross",
}
for _factor_type, _factor_definition in FACTOR_REGISTRY.items():
    _guide = _FACTOR_FAMILY_GUIDES[_factor_definition["family"]]
    _direction_text = "数值越小越符合默认偏好" if _factor_definition.get("direction") == "lower" else "数值越大越符合默认偏好"
    if _factor_type in _BINARY_FACTOR_TYPES:
        _direction_text = "条件发生当日输出 1，未发生输出 0；它表示事件，不代表收益保证"
    _factor_definition.update({
        "plain_explanation": f"{_factor_definition['description']} 计算只使用当前及此前已经完成的日线。",
        "how_to_read": f"{_direction_text}。组合使用时还会做每日横截面标准化，再乘以节点权重。",
        "best_for": _guide["best_for"],
        "pitfall": _guide["pitfall"],
        "example_caption": _guide["visual"],
    })

_REMOVED_NODE_TYPES = ACTIVE_MARKET_GATE_BLOCK_TYPES | Z_ENTRY_BLOCK_TYPES | {
    "strategy_z_exit_risk_guard", "factor_zettaranc_quality", "factor_z_resonance",
    "factor_brick", "penalty_delta_rp",
    "factor_industry_concept_trend", "factor_cyc", "factor_fundamental_ai",
    "factor_machine_learning",
}
for _removed_type in _REMOVED_NODE_TYPES:
    BLOCK_CATALOG.pop(_removed_type, None)
for _factor_type, _definition in FACTOR_REGISTRY.items():
    if _factor_type in _REMOVED_NODE_TYPES:
        continue
    _factor_params = {
        **dict(_definition.get("params") or {}),
        "weight": 0.10,
        "direction": _definition.get("direction", "higher"),
        "method": "robust_sigmoid",
    }
    BLOCK_CATALOG[_factor_type] = _entry(
        "factor", _definition["name"], _definition["description"], _factor_params,
        family=_definition["family"], formula=_definition["formula"],
        required_fields=["daily_bar:OHLCV+amount"],
        output="连续横截面评分", point_in_time=True,
        plain_explanation=_definition["plain_explanation"],
        how_to_read=_definition["how_to_read"],
        best_for=_definition["best_for"], pitfall=_definition["pitfall"],
        example_caption=_definition["example_caption"],
    )


@dataclass(frozen=True)
class BlockSpec:
    id: str
    type: str
    category: str = ""
    name: str = ""
    enabled: bool = True
    params: dict[str, Any] = field(default_factory=dict)
    combine: str = "AND"


@dataclass(frozen=True)
class PipelineSpec:
    id: str
    name: str
    description: str
    version: int
    scope: str
    start: str
    end: str
    initial_cash: float
    blocks: list[BlockSpec]
    fees: dict[str, float] = field(default_factory=dict)
    workflow: dict[str, Any] = field(default_factory=dict)


_CUSTOM_FACTOR_FIELDS = {
    "open", "high", "low", "close", "volume", "amount", "prev_close",
    "daily_return", "listing_days",
}
_CUSTOM_FACTOR_PERIOD_FUNCTIONS = {
    "MOM": ("momentum", 1, "close/nullif(lag_close_{n},0)-1"),
    "MA": ("ma_periods", 1, "ma_{n}"),
    "RSI": ("rsis", 2, "rsi_{n}"),
    "VOL": ("vols", 2, "vol_{n}"),
    "ADV": ("liquidities", 1, "adv_{n}"),
    "DRAWDOWN": ("drawdowns", 2, "close/nullif(peak_{n},0)-1"),
}
_CUSTOM_FORMULA_BLOCKS = {
    "filter_custom_formula": True,
    "custom_formula": True,
    "factor_custom_formula": False,
    "penalty_custom_formula": False,
}


class _CustomFactorCompiler(ast.NodeVisitor):
    def __init__(self, *, boolean: bool = False) -> None:
        self.boolean = boolean
        self.requirements = {
            key: set() for key in (
                "momentum", "ma_periods", "rsis", "vols", "liquidities", "drawdowns"
            )
        }
        self.node_count = 0

    def compile(self, formula: str) -> tuple[str, dict[str, set[int]]]:
        text = formula.strip()
        if not text:
            raise ValueError("自定义因子公式不能为空")
        if len(text) > 500:
            raise ValueError("自定义因子公式不能超过 500 个字符")
        try:
            tree = ast.parse(text, mode="eval")
        except SyntaxError as exc:
            raise ValueError(f"自定义因子公式语法错误：{exc.msg}") from exc
        sql = self.visit(tree)
        if self.node_count > 100:
            raise ValueError("自定义因子公式过于复杂，最多允许 100 个表达式节点")
        return sql, self.requirements

    def visit(self, node: ast.AST) -> str:
        self.node_count += 1
        return super().visit(node)

    def visit_Expression(self, node: ast.Expression) -> str:
        return self.visit(node.body)

    def visit_Name(self, node: ast.Name) -> str:
        field = node.id.lower()
        if field not in _CUSTOM_FACTOR_FIELDS:
            raise ValueError(f"自定义因子不支持字段：{node.id}")
        return field

    def visit_Constant(self, node: ast.Constant) -> str:
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("自定义因子只允许数值常量")
        value = float(node.value)
        if not math.isfinite(value):
            raise ValueError("自定义因子常量必须是有限数值")
        return repr(value)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> str:
        if self.boolean and isinstance(node.op, ast.Not):
            return f"(NOT ({self.visit(node.operand)}))"
        if not isinstance(node.op, (ast.UAdd, ast.USub)):
            raise ValueError("自定义因子只允许正负号一元运算")
        operator = "+" if isinstance(node.op, ast.UAdd) else "-"
        return f"({operator}{self.visit(node.operand)})"

    def visit_BinOp(self, node: ast.BinOp) -> str:
        left, right = self.visit(node.left), self.visit(node.right)
        if isinstance(node.op, ast.Add):
            return f"(({left})+({right}))"
        if isinstance(node.op, ast.Sub):
            return f"(({left})-({right}))"
        if isinstance(node.op, ast.Mult):
            return f"(({left})*({right}))"
        if isinstance(node.op, ast.Div):
            return f"(({left})/nullif(({right}),0))"
        raise ValueError("自定义因子只允许 +、-、*、/ 运算")

    def visit_BoolOp(self, node: ast.BoolOp) -> str:
        if not self.boolean or not isinstance(node.op, (ast.And, ast.Or)):
            raise ValueError("数值因子公式不允许布尔运算")
        operator = "AND" if isinstance(node.op, ast.And) else "OR"
        return "(" + f" {operator} ".join(f"({self.visit(value)})" for value in node.values) + ")"

    def visit_Compare(self, node: ast.Compare) -> str:
        if not self.boolean:
            raise ValueError("数值因子公式不允许比较运算")
        operators = {
            ast.Gt: ">", ast.GtE: ">=", ast.Lt: "<", ast.LtE: "<=",
            ast.Eq: "=", ast.NotEq: "!=",
        }
        left = self.visit(node.left)
        parts = []
        for operator_node, comparator in zip(node.ops, node.comparators):
            operator = operators.get(type(operator_node))
            if operator is None:
                raise ValueError("自定义条件只允许 >、>=、<、<=、==、!= 比较")
            right = self.visit(comparator)
            parts.append(f"(({left}) {operator} ({right}))")
            left = right
        return "(" + " AND ".join(parts) + ")"

    def visit_Call(self, node: ast.Call) -> str:
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise ValueError("自定义因子函数必须使用简单函数名且不支持关键字参数")
        name = node.func.id.upper()
        if name in _CUSTOM_FACTOR_PERIOD_FUNCTIONS:
            if len(node.args) != 1 or not isinstance(node.args[0], ast.Constant):
                raise ValueError(f"{name} 需要一个整数周期，例如 {name}(20)")
            raw_period = node.args[0].value
            if isinstance(raw_period, bool) or not isinstance(raw_period, int):
                raise ValueError(f"{name} 的周期必须是整数")
            requirement, minimum, template = _CUSTOM_FACTOR_PERIOD_FUNCTIONS[name]
            period = int(raw_period)
            if not minimum <= period <= 500:
                raise ValueError(f"{name} 的周期必须在 {minimum} 到 500 之间")
            self.requirements[requirement].add(period)
            return template.format(n=period)
        args = [self.visit(arg) for arg in node.args]
        if name == "ABS" and len(args) == 1:
            return f"abs({args[0]})"
        if name == "LOG" and len(args) == 1:
            return f"CASE WHEN ({args[0]})>0 THEN ln({args[0]}) END"
        if name == "SQRT" and len(args) == 1:
            return f"CASE WHEN ({args[0]})>=0 THEN sqrt({args[0]}) END"
        if name in {"MIN", "MAX"} and 2 <= len(args) <= 8:
            function = "least" if name == "MIN" else "greatest"
            return f"{function}({', '.join(args)})"
        raise ValueError(
            f"自定义因子不支持函数：{name}；可用 MOM/MA/RSI/VOL/ADV/DRAWDOWN/ABS/LOG/SQRT/MIN/MAX"
        )

    def generic_visit(self, node: ast.AST) -> str:
        raise ValueError(f"自定义因子包含不允许的语法：{type(node).__name__}")


def _compile_custom_formula(
    formula: str, *, boolean: bool = False
) -> tuple[str, dict[str, set[int]]]:
    return _CustomFactorCompiler(boolean=boolean).compile(formula)


def _compile_custom_factor(formula: str) -> tuple[str, dict[str, set[int]]]:
    return _compile_custom_formula(formula)


def validate_custom_formula(formula: str, block_type: str) -> dict[str, Any]:
    if block_type not in _CUSTOM_FORMULA_BLOCKS:
        raise ValueError("只支持校验自定义筛选、策略、因子和风险惩罚公式")
    expression, requirements = _compile_custom_formula(
        formula,
        boolean=_CUSTOM_FORMULA_BLOCKS[block_type],
    )
    return {
        "ok": True,
        "mode": "condition" if _CUSTOM_FORMULA_BLOCKS[block_type] else "numeric",
        "expression": expression,
        "requirements": {
            key: sorted(values) for key, values in requirements.items()
        },
    }


def _block(block: BlockSpec | dict[str, Any]) -> BlockSpec:
    raw = asdict(block) if isinstance(block, BlockSpec) else dict(block)
    block_type = str(raw.get("type", ""))
    catalog = BLOCK_CATALOG.get(block_type, {})
    defaults = dict(catalog.get("params", {}))
    defaults.update(dict(raw.get("params") or {}))
    combine = str(raw.get("combine", "AND")).upper()
    # The active-market gate contains its own OR exit rule (price below MA or
    # one-day change <= exit_one).  At pipeline level it is a gate/filter and
    # must be joined with AND.  Normalize legacy/unsaved OR values instead of
    # rejecting the whole workflow as "the first block cannot use OR".
    if block_type in ACTIVE_MARKET_GATE_BLOCK_TYPES:
        combine = "AND"
    return BlockSpec(
        id=str(raw.get("id") or f"{block_type}-{uuid.uuid4().hex[:8]}"),
        type=block_type,
        category=str(raw.get("category") or catalog.get("category", "")),
        name=str(raw.get("name") or catalog.get("name", block_type)),
        enabled=bool(raw.get("enabled", True)),
        params=defaults,
        combine=combine,
    )


def _coerce_pipeline(spec: PipelineSpec | dict[str, Any]) -> PipelineSpec:
    if isinstance(spec, PipelineSpec):
        raw = asdict(spec)
    else:
        raw = dict(spec)
    return PipelineSpec(
        id=str(raw.get("id") or f"pipeline-{uuid.uuid4().hex[:10]}"),
        name=str(raw.get("name") or "未命名乐高流水线"),
        description=str(raw.get("description") or ""),
        version=int(raw.get("version", 1)),
        scope=str(raw.get("scope") or "all"),
        start=str(raw.get("start") or ""),
        end=str(raw.get("end") or ""),
        initial_cash=float(raw.get("initial_cash", 1_000_000.0)),
        blocks=[_block(item) for item in raw.get("blocks", [])],
        fees={str(key): float(value) for key, value in dict(raw.get("fees") or {}).items()},
        workflow=dict(raw.get("workflow") or {}),
    )


def _pipeline_hash(spec: PipelineSpec) -> str:
    payload = asdict(spec)
    # Canvas coordinates are presentation state, not strategy semantics.  A
    # node drag must not change the executable strategy hash.
    for key in ("id", "name", "description", "workflow"):
        payload.pop(key, None)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _market_end() -> pd.Timestamp:
    if MARKET_DB.exists():
        try:
            with duckdb.connect(str(MARKET_DB), read_only=True) as connection:
                value = connection.execute("SELECT max(date) FROM daily_bar").fetchone()[0]
                if value:
                    return pd.Timestamp(value)
        except Exception:
            pass
    return pd.Timestamp.today().normalize()


def default_pipeline() -> PipelineSpec:
    end = _market_end()
    start = end - pd.DateOffset(years=1)
    blocks = [
        BlockSpec("universe", "universe", params={"scope": "all", "codes": []}),
        BlockSpec("non-st", "filter_non_st"),
        BlockSpec("tradable", "filter_tradable"),
        BlockSpec("not-limit-up", "filter_not_limit_up"),
        BlockSpec("listing", "filter_listing_days"),
        BlockSpec("adv20", "filter_adv20"),
        BlockSpec("momentum-gate", "strategy_momentum"),
        BlockSpec("factor-momentum", "factor_momentum"),
        BlockSpec("factor-trend", "factor_trend"),
        BlockSpec("factor-rsi", "factor_rsi"),
        BlockSpec("factor-low-vol", "factor_low_volatility"),
        BlockSpec("factor-liquidity", "factor_liquidity"),
        BlockSpec("factor-drawdown", "factor_drawdown"),
        BlockSpec("normalizer", "normalize_cross_section"),
        BlockSpec("penalty-vol", "penalty_volatility"),
        BlockSpec("penalty-drawdown", "penalty_drawdown"),
        BlockSpec("penalty-liquidity", "penalty_liquidity"),
        BlockSpec("rank", "rank_topk"),
        BlockSpec("portfolio", "portfolio"),
        BlockSpec("execution", "execution_next_open"),
    ]
    return _coerce_pipeline(PipelineSpec(
        id="default-full-market-multifactor",
        name="全市场多因子乐高流水线",
        description="硬筛 → 动量初选 → 多因子/风险惩罚 → 每日排名 → 组合 → 次日开盘模拟",
        version=1,
        scope="all",
        start=start.date().isoformat(),
        end=end.date().isoformat(),
        initial_cash=1_000_000.0,
        blocks=blocks,
        fees={},
        workflow={},
    ))


def standard_multifactor_pipeline() -> PipelineSpec:
    """Plain point-in-time multi-factor research preset without external gates."""
    end = _market_end()
    start = max(pd.Timestamp("2023-01-01"), end - pd.DateOffset(years=3))
    blocks = [
        BlockSpec("universe", "universe", params={"scope": "all", "codes": []}),
        BlockSpec("non-st", "filter_non_st"),
        BlockSpec("tradable", "filter_tradable"),
        BlockSpec("not-limit-up", "filter_not_limit_up"),
        BlockSpec("listing", "filter_listing_days", params={"min_days": 120}),
        BlockSpec("adv20", "filter_adv20", params={"window": 20, "min_amount": 30_000_000.0}),
        BlockSpec(
            "momentum-gate", "strategy_momentum",
            params={"group": "strategy", "lookback": 60, "min_return": 0.0},
        ),
        BlockSpec(
            "factor-momentum", "factor_momentum",
            params={"lookback": 60, "weight": 0.60, "direction": "higher", "method": "percentile"},
        ),
        BlockSpec(
            "factor-trend", "factor_trend",
            params={"fast": 10, "slow": 60, "weight": 0.25, "direction": "higher", "method": "percentile"},
        ),
        BlockSpec(
            "factor-liquidity", "factor_liquidity",
            params={"window": 20, "weight": 0.15, "direction": "lower", "method": "percentile"},
        ),
        BlockSpec("normalizer", "normalize_cross_section"),
        BlockSpec("rank", "rank_topk", params={"top_k": 20}),
        BlockSpec(
            "portfolio", "portfolio",
            params={"positions": 8, "rebalance_every": 10, "weighting": "equal"},
        ),
        BlockSpec("execution", "execution_next_open", params={
            "mode": "next_open", "commission": 0.0003, "min_commission": 5.0,
            "transfer_fee": 0.00001, "stamp_tax": 0.0005, "slippage": 0.0005,
            "lot_size": 100, "invest_ratio": 0.80, "max_participation": 0.05,
        }),
    ]
    return _coerce_pipeline(PipelineSpec(
        id="standard-robust-multifactor",
        name="标准稳健多因子",
        description=(
            "在通过ST、上市天数、可交易性和流动性硬筛的股票中，"
            "按60日动量、10/60日趋势和流动性横截面排序。"
            "T日收盘冻结，T+1开盘模拟，8只等权且总仓不超过80%。"
        ),
        version=1,
        scope="all",
        start=start.date().isoformat(),
        end=end.date().isoformat(),
        initial_cash=1_000_000.0,
        blocks=blocks,
        fees={},
        workflow={},
    ))


def blank_pipeline() -> PipelineSpec:
    end = _market_end()
    start = end - pd.DateOffset(years=1)
    return PipelineSpec(
        id=f"pipeline-{uuid.uuid4().hex[:10]}",
        name="未命名工作流",
        description="",
        version=1,
        scope="all",
        start=start.date().isoformat(),
        end=end.date().isoformat(),
        initial_cash=1_000_000.0,
        blocks=[],
        fees={},
        workflow={"version": 1, "nodes": [], "edges": []},
    )


def validate_pipeline_draft(spec: PipelineSpec | dict[str, Any]) -> PipelineSpec:
    pipeline = _coerce_pipeline(spec)
    if pipeline.initial_cash <= 0:
        raise ValueError("初始资金必须大于 0")
    try:
        start = pd.Timestamp(pipeline.start).normalize()
        end = pd.Timestamp(pipeline.end).normalize()
    except Exception as exc:
        raise ValueError("开始和结束日期必须是有效日期") from exc
    if end < start:
        raise ValueError("结束日期不能早于开始日期")
    ids: set[str] = set()
    for block in pipeline.blocks:
        if block.id in ids:
            raise ValueError(f"积木 id 重复：{block.id}")
        ids.add(block.id)
        if block.type not in BLOCK_CATALOG:
            raise ValueError(f"不支持的积木类型：{block.type}")
    from .lego_workflow import default_workflow, normalize_workflow

    raw_workflow = pipeline.workflow or {}
    if pipeline.blocks and not raw_workflow.get("nodes") and not raw_workflow.get("edges"):
        raw_workflow = default_workflow([asdict(block) for block in pipeline.blocks])
    workflow = normalize_workflow(
        raw_workflow or {"version": 1, "nodes": [], "edges": []},
        [asdict(block) for block in pipeline.blocks],
    )
    return PipelineSpec(
        id=pipeline.id,
        name=pipeline.name,
        description=pipeline.description,
        version=pipeline.version,
        scope=pipeline.scope,
        start=pipeline.start,
        end=pipeline.end,
        initial_cash=pipeline.initial_cash,
        blocks=pipeline.blocks,
        fees=pipeline.fees,
        workflow=workflow,
    )


def validate_pipeline(spec: PipelineSpec | dict[str, Any]) -> PipelineSpec:
    pipeline = validate_pipeline_draft(spec)
    if not pipeline.blocks:
        raise ValueError("流水线至少需要一个积木")
    from .lego_workflow import compile_workflow

    ordered, workflow = compile_workflow(
        [asdict(block) for block in pipeline.blocks], pipeline.workflow
    )
    pipeline = PipelineSpec(
        id=pipeline.id,
        name=pipeline.name,
        description=pipeline.description,
        version=pipeline.version,
        scope=pipeline.scope,
        start=pipeline.start,
        end=pipeline.end,
        initial_cash=pipeline.initial_cash,
        blocks=[_block(block) for block in ordered],
        fees=pipeline.fees,
        workflow=workflow,
    )
    ids: set[str] = set()
    last_stage = -1
    enabled_counts = {category: 0 for category in STAGE_ORDER}
    seen_groups: set[tuple[str, str]] = set()
    active_group: tuple[str, str] | None = None
    factor_weight = 0.0
    for index, block in enumerate(pipeline.blocks):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", block.id):
            raise ValueError(f"积木 #{index + 1} 的 id 只能包含字母、数字、下划线和连字符")
        if block.id in ids:
            raise ValueError(f"积木 id 重复: {block.id}")
        ids.add(block.id)
        if block.type not in BLOCK_CATALOG:
            raise ValueError(f"未知积木类型: {block.type}")
        catalog = BLOCK_CATALOG[block.type]
        if block.category != catalog["category"]:
            raise ValueError(f"{block.name} 的阶段必须是 {catalog['category']}")
        if block.combine not in {"AND", "OR"}:
            raise ValueError(f"{block.name} 的 combine 只能是 AND 或 OR")
        if not block.enabled:
            continue
        if not catalog["available"]:
            raise ValueError(f"{block.name} 当前不可启用：{catalog['disabled_reason']}")
        stage = STAGE_ORDER[block.category]
        if stage < last_stage:
            raise ValueError(
                f"阶段顺序错误：{block.name}({block.category}) 不能放在后续阶段之后；"
                "必须按股票池→筛选→策略→因子→惩罚→排名→组合→执行排列"
            )
        last_stage = stage
        enabled_counts[block.category] += 1
        if block.category in {"filter", "strategy"}:
            group = str(block.params.get("group") or block.id)
            group_key = (block.category, group)
            if group_key != active_group:
                if group_key in seen_groups:
                    raise ValueError(f"筛选组 {group} 必须连续放置")
                if block.combine == "OR":
                    raise ValueError(f"筛选组 {group} 的第一块不能使用 OR")
                seen_groups.add(group_key)
                active_group = group_key
            elif block.combine not in {"AND", "OR"}:
                raise ValueError(f"筛选组 {group} 的组合方式无效")
        else:
            active_group = None
        params = block.params
        for key in (
            "lookback", "fast", "slow", "period", "window", "entry",
            "short_period", "long_period", "min_gap", "max_gap", "ma_period",
        ):
            if key in params:
                try:
                    numeric = int(params[key])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{block.name}.{key} 必须是整数") from exc
                if numeric <= 0:
                    raise ValueError(f"{block.name}.{key} 必须大于 0")
        if block.category == "universe":
            scope = str(params.get("scope", pipeline.scope))
            if scope not in ENABLED_UNIVERSE_SCOPES:
                raise ValueError("数据源预设当前不可执行")
            codes = [str(code).strip() for code in params.get("codes", [])]
            if scope == "custom" and not codes:
                raise ValueError("自定义股票池不能为空")
            if any(not re.fullmatch(r"\d{6}", code) for code in codes):
                raise ValueError("自定义股票代码必须是 6 位数字")
        if block.type == "filter_listing_days" and int(params.get("min_days", 0)) < 0:
            raise ValueError("上市天数不能为负")
        if block.type == "filter_adv20" and float(params.get("min_amount", 0)) < 0:
            raise ValueError("成交额门槛不能为负")
        if block.type in {"strategy_ma_cross", "factor_trend"} and int(params.get("slow", 20)) <= int(params.get("fast", 5)):
            raise ValueError(f"{block.name} 的 slow 必须大于 fast")
        if block.type == "strategy_zettaranc_b1":
            if float(params.get("min_gain", -2.0)) > float(params.get("max_gain", 1.8)):
                raise ValueError("Z·B1最小涨幅不能大于最大涨幅")
            for key in ("main_max_amplitude", "growth_max_amplitude", "max_volume_ratio"):
                if not math.isfinite(float(params.get(key, 0))) or float(params.get(key, 0)) <= 0:
                    raise ValueError(f"Z·B1参数 {key} 必须是正数")
            n_lift_floor = float(params.get("n_lift_floor", 0.98))
            if not 0.5 <= n_lift_floor <= 1.2:
                raise ValueError("Z·B1 N型低点比例必须在 0.5 到 1.2 之间")
        if block.type == "strategy_z_b123" and not any(
            bool(params.get(key, True)) for key in ("allow_b1", "allow_b2", "allow_b3")
        ):
            raise ValueError("Z·B1/B2/B3 三阶段准入至少启用一个阶段")
        if block.type in ACTIVE_MARKET_GATE_BLOCK_TYPES:
            if int(params.get("max_gap_days", 0)) < 0:
                raise ValueError("活跃市值 max_gap_days 不能为负")
            for key in ("start_one", "start_two", "exit_one"):
                if not math.isfinite(float(params.get(key, 0))):
                    raise ValueError(f"活跃市值 {key} 必须是有限数值")
            ma_buffer = float(params.get("ma_buffer", 0.0))
            if not math.isfinite(ma_buffer) or not 0 <= ma_buffer <= 0.10:
                raise ValueError("活跃市值 ma_buffer 必须在 0 到 0.10 之间")
            expected_provider = (
                "ths_internal_proxy"
                if block.type == "filter_ths_active_market_proxy_gate"
                else "compass"
            )
            if str(params.get("provider", expected_provider)) != expected_provider:
                raise ValueError(f"{block.name} 的 provider 必须是 {expected_provider}")
        if block.type in _CUSTOM_FORMULA_BLOCKS:
            factor_name = str(params.get("name") or block.name).strip()
            if not 1 <= len(factor_name) <= 40:
                raise ValueError("自定义节点名称长度必须为 1 到 40 个字符")
            _compile_custom_formula(
                str(params.get("formula") or ""),
                boolean=_CUSTOM_FORMULA_BLOCKS[block.type],
            )
        if block.type.startswith("factor_"):
            weight = float(params.get("weight", 0))
            if not math.isfinite(weight) or weight < 0:
                raise ValueError(f"{block.name} 权重不能为负")
            factor_weight += weight
            if params.get("direction", "higher") not in {"higher", "lower"}:
                raise ValueError(f"{block.name} direction 只能是 higher 或 lower")
            if params.get("method", "robust_sigmoid") not in {"robust_sigmoid", "percentile"}:
                raise ValueError(f"{block.name} method 只能是 robust_sigmoid 或 percentile")
        if block.type.startswith("penalty_"):
            if not math.isfinite(float(params.get("weight", 0))) or float(params.get("weight", 0)) < 0:
                raise ValueError(f"{block.name} 权重不能为负")
            if params.get("method", "percentile") not in {"robust_sigmoid", "percentile"}:
                raise ValueError(f"{block.name} method 只能是 robust_sigmoid 或 percentile")
        if block.type == "normalize_cross_section":
            if not 0 < float(params.get("sigmoid_k", 1.0)) <= 20:
                raise ValueError("S 形斜率 sigmoid_k 必须在 0 到 20 之间")
            if not 0.5 <= float(params.get("clip", 6.0)) <= 20:
                raise ValueError("标准化截断 clip 必须在 0.5 到 20 之间")
            if params.get("winsorize", "median_mad") != "median_mad":
                raise ValueError("V1 标准化仅支持 median_mad，不得静默替换")
    for category in ("universe", "normalize", "rank", "portfolio", "execution"):
        if enabled_counts[category] != 1:
            raise ValueError(f"流水线必须且只能启用一个 {category} 积木")
    if factor_weight <= 0:
        raise ValueError("至少启用一个权重大于 0 的多因子积木")
    rank = next(block for block in pipeline.blocks if block.enabled and block.category == "rank")
    portfolio = next(block for block in pipeline.blocks if block.enabled and block.category == "portfolio")
    top_k = int(rank.params.get("top_k", 50))
    positions = int(portfolio.params.get("positions", 10))
    rebalance = int(portfolio.params.get("rebalance_every", 5))
    if not 1 <= top_k <= 500:
        raise ValueError("Top K 必须在 1 到 500 之间")
    if not 1 <= positions <= min(top_k, 200):
        raise ValueError("组合持仓数必须为 1 至 Top K，且不能超过 200")
    if not 1 <= rebalance <= 252:
        raise ValueError("调仓间隔必须在 1 到 252 个交易日之间")
    if portfolio.params.get("weighting", "equal") != "equal":
        raise ValueError("V1 只启用等权组合；其他权重方法不得静默替代")
    execution = next(block for block in pipeline.blocks if block.enabled and block.category == "execution")
    if execution.type != "execution_next_open":
        raise ValueError("当前真实可用的执行积木只有次日开盘")
    if sum(1 for block in pipeline.blocks if block.enabled and block.type in ACTIVE_MARKET_GATE_BLOCK_TYPES) > 1:
        raise ValueError("同一流水线最多启用一个活跃市值市场门控")
    enabled_types = {block.type for block in pipeline.blocks if block.enabled}
    z_entry_types = Z_ENTRY_BLOCK_TYPES & enabled_types
    if z_entry_types and "filter_ths_active_market_proxy_gate" not in enabled_types:
        raise ValueError("Z·准入节点必须与同花顺活跃市值拟合门控串联，禁止单独绕过市场门控")
    if len(z_entry_types) > 1:
        raise ValueError("同一流水线只能启用一个Z·B1/B2/B3准入策略")
    if "factor_zettaranc_quality" in enabled_types and not z_entry_types:
        raise ValueError("Z·结构质量因子只能用于已经通过Z·硬筛的流水线")
    if "factor_z_resonance" in enabled_types and not (Z_STANDARD_STAGE_BLOCK_TYPES & enabled_types):
        raise ValueError("Z·阶段共振因子只能用于已经通过标准B1/B2/B3准入的流水线")
    if "strategy_z_exit_risk_guard" in enabled_types and not z_entry_types:
        raise ValueError("Z·退出风险剔除只能放在Z·准入策略之后")
    if "strategy_z_exit_risk_guard" in enabled_types and z_entry_types:
        ordered_types = [block.type for block in pipeline.blocks if block.enabled]
        entry_type = next(iter(z_entry_types))
        if ordered_types.index("strategy_z_exit_risk_guard") < ordered_types.index(entry_type):
            raise ValueError("Z·退出风险剔除必须位于Z·准入策略之后")
    fee_values = {
        "commission": 0.0003, "min_commission": 5.0, "transfer_fee": 0.00001,
        "stamp_tax": 0.0005, "slippage": 0.0005,
        "invest_ratio": 0.95, "max_position_pct": 0.10, "max_participation": 0.10,
        **pipeline.fees, **execution.params,
    }
    for key in ("commission", "min_commission", "transfer_fee", "stamp_tax", "slippage"):
        if not math.isfinite(float(fee_values[key])) or float(fee_values[key]) < 0:
            raise ValueError(f"费用参数 {key} 不能为负")
    if float(fee_values["slippage"]) > 0.20:
        raise ValueError("slippage 不能超过 20%")
    for key in ("invest_ratio", "max_position_pct", "max_participation"):
        if not math.isfinite(float(fee_values[key])) or not 0 < float(fee_values[key]) <= 1:
            raise ValueError(f"{key} 必须在 0 到 1 之间")
    if int(fee_values.get("lot_size", 100)) <= 0:
        raise ValueError("lot_size 必须为正整数")
    # Deprecated top-level fees are still validated (new pipelines should edit
    # the execution block).  Invalid legacy values must never be ignored.
    for key, value in pipeline.fees.items():
        if key in {"commission", "min_commission", "transfer_fee", "stamp_tax", "slippage"} and float(value) < 0:
            raise ValueError(f"费用参数 {key} 不能为负")
        if key in {"invest_ratio", "max_position_pct", "max_participation"} and not 0 < float(value) <= 1:
            raise ValueError(f"{key} 必须在 0 到 1 之间")
    universe_scope = str(next(
        block for block in pipeline.blocks if block.enabled and block.category == "universe"
    ).params.get("scope", pipeline.scope))
    # The universe block is the single source of truth; keep the top-level
    # summary synchronized for persistence and old UI consumers.
    return PipelineSpec(
        id=pipeline.id, name=pipeline.name, description=pipeline.description,
        version=pipeline.version, scope=universe_scope, start=pipeline.start,
        end=pipeline.end, initial_cash=pipeline.initial_cash,
        blocks=pipeline.blocks, fees=pipeline.fees, workflow=pipeline.workflow,
    )


def _enabled(spec: PipelineSpec, category: str | None = None) -> list[BlockSpec]:
    return [
        block for block in spec.blocks
        if block.enabled and (category is None or block.category == category)
    ]


def _one(spec: PipelineSpec, category: str) -> BlockSpec:
    return _enabled(spec, category)[0]


def _scope_clause(universe: BlockSpec) -> tuple[str, str, str]:
    scope = str(universe.params.get("scope", "all"))
    if scope == "all":
        return scope, "m.exchange IN ('SH', 'SZ', 'BJ')", "stock"
    if scope == "sh_sz":
        return scope, "m.exchange IN ('SH', 'SZ')", "stock"
    if scope == "sh":
        return scope, "m.exchange='SH'", "stock"
    if scope == "sz":
        return scope, "m.exchange='SZ'", "stock"
    if scope == "bj":
        return scope, "m.exchange='BJ'", "stock"
    if scope == "main":
        return scope, (
            "(m.exchange='SH' AND regexp_matches(m.code, '^(600|601|603|605)')) OR "
            "(m.exchange='SZ' AND regexp_matches(m.code, '^(000|001|002|003)'))"
        ), "stock"
    if scope == "star":
        return scope, "m.exchange='SH' AND m.code LIKE '688%'", "stock"
    if scope == "chinext":
        return scope, "m.exchange='SZ' AND regexp_matches(m.code, '^(300|301)')", "stock"
    if scope == "etf":
        return scope, (
            "m.exchange IN ('SH','SZ') AND (upper(coalesce(m.name,'')) LIKE '%ETF%' OR "
            "regexp_matches(m.code, '^(159|510|511|512|513|515|516|517|518|520|560|561|562|563|588)'))"
        ), "fund"
    codes = sorted(set(str(code) for code in universe.params.get("codes", [])))
    values = ",".join(f"'{code}'" for code in codes)
    return scope, f"m.code IN ({values})", "mixed"


def _int_param(block: BlockSpec, key: str, default: int, minimum: int = 1) -> int:
    return max(minimum, int(block.params.get(key, default)))


def _asset_clause(asset_type: str) -> str:
    if asset_type == "mixed":
        return "m.asset_type IN ('stock','fund')"
    if asset_type not in {"stock", "fund"}:
        raise ValueError("数据源资产类型无效")
    return f"m.asset_type='{asset_type}'"


def _requirements(spec: PipelineSpec) -> dict[str, Any]:
    momentum = {20}
    mas = {(5, 20)}
    ma_periods = {5, 20}
    rsis = {14}
    vols = {20}
    liquidities = {20}
    drawdowns = {60}
    turtles = {20}
    rolling = {14, 20, 60}
    ema_periods: set[int] = set()
    ma_lags: set[tuple[int, int]] = set()
    obv_windows: set[int] = set()
    recursive_indicators = False
    for block in _enabled(spec):
        if block.type in {"strategy_momentum", "factor_momentum"}:
            momentum.add(_int_param(block, "lookback", 20))
        elif block.type in {"strategy_ma_cross", "factor_trend"}:
            fast = _int_param(block, "fast", 5)
            slow = max(fast + 1, _int_param(block, "slow", 20))
            mas.add((fast, slow))
            ma_periods.update((fast, slow))
        elif block.type in {"strategy_rsi", "factor_rsi"}:
            rsis.add(_int_param(block, "period", 14, 2))
        elif block.type in {"factor_low_volatility", "penalty_volatility"}:
            vols.add(_int_param(block, "window", 20, 2))
        elif block.type in {"filter_adv20", "factor_liquidity", "penalty_liquidity"}:
            liquidities.add(_int_param(block, "window", 20))
        elif block.type in {"factor_drawdown", "penalty_drawdown"}:
            drawdowns.add(_int_param(block, "window", 60, 2))
        elif block.type == "strategy_turtle":
            turtles.add(_int_param(block, "entry", 20, 2))
        elif block.type in FACTOR_REGISTRY:
            params = block.params
            for key in ("lookback", "period", "window", "fast", "medium", "slow", "short", "long"):
                if key in params:
                    value = _int_param(block, key, 20, 1)
                    rolling.add(value)
            if block.type in {"factor_multi_momentum"}:
                momentum.update(_int_param(block, key, default, 1) for key, default in (("short", 5), ("medium", 20), ("long", 60)))
            elif block.type in {"factor_momentum", "factor_short_reversal"}:
                momentum.add(_int_param(block, "lookback", 20, 1))
            elif block.type in {"factor_trend_efficiency", "factor_crowding_proxy"}:
                momentum.add(_int_param(block, "window", 20, 2))
            if block.type in {"factor_trend", "factor_ma_alignment"}:
                ma_periods.update(
                    _int_param(block, key, default, 1)
                    for key, default in (("fast", 5), ("medium", 10), ("slow", 20))
                    if key in params or key != "medium" or block.type == "factor_ma_alignment"
                )
            if block.type in {"factor_ma_position", "factor_ma_cross_event", "factor_ma_breakdown_event", "factor_ma_deviation", "factor_ma_slope"}:
                period = _int_param(block, "period", 20, 1)
                ma_periods.add(period)
                if block.type == "factor_ma_slope":
                    ma_lags.add((period, _int_param(block, "slope_days", 5, 1)))
                elif block.type in {"factor_ma_cross_event", "factor_ma_breakdown_event"}:
                    ma_lags.add((period, 1))
            if block.type == "factor_ema_cross":
                ema_periods.update((_int_param(block, "fast", 12, 1), _int_param(block, "slow", 26, 2)))
                recursive_indicators = True
            if block.type.startswith("factor_macd_"):
                ema_periods.update((12, 26))
                recursive_indicators = True
            if block.type in {"factor_kdj_k", "factor_kdj_j", "factor_brick"}:
                recursive_indicators = True
            if block.type == "factor_obv_momentum":
                window = _int_param(block, "window", 20, 1)
                obv_windows.add(window)
                recursive_indicators = True
            if block.type == "factor_rsi":
                rsis.add(_int_param(block, "period", 14, 2))
            if block.type in {"factor_low_volatility"}:
                vols.add(_int_param(block, "window", 20, 2))
            if block.type in {"factor_liquidity", "factor_amount_ratio", "factor_liquidity_shock"}:
                liquidities.add(_int_param(block, "window", 20, 1))
            if block.type == "factor_drawdown":
                drawdowns.add(_int_param(block, "window", 60, 2))
        elif block.type in _CUSTOM_FORMULA_BLOCKS:
            _, custom = _compile_custom_formula(
                str(block.params.get("formula") or ""),
                boolean=_CUSTOM_FORMULA_BLOCKS[block.type],
            )
            momentum.update(custom["momentum"])
            ma_periods.update(custom["ma_periods"])
            rsis.update(custom["rsis"])
            vols.update(custom["vols"])
            liquidities.update(custom["liquidities"])
            drawdowns.update(custom["drawdowns"])
    rolling.update(momentum | ma_periods | rsis | vols | liquidities | drawdowns | turtles)
    longest = max(
        [*rolling, *momentum, *ma_periods, *rsis, *vols,
         *liquidities, *drawdowns, *turtles,
         *(period + lag for period, lag in ma_lags)]
    )
    return {
        "momentum": sorted(momentum), "mas": sorted(mas), "ma_periods": sorted(ma_periods),
        "rsis": sorted(rsis),
        "vols": sorted(vols), "liquidities": sorted(liquidities),
        "drawdowns": sorted(drawdowns), "turtles": sorted(turtles),
        "rolling": sorted(rolling), "ema_periods": sorted(ema_periods),
        "ma_lags": sorted(ma_lags), "obv_windows": sorted(obv_windows),
        "recursive_indicators": recursive_indicators,
        "longest": longest,
    }


def _feature_sql(
    scope_clause: str,
    req: dict[str, Any],
    calendar_scope_clause: str | None = None,
    asset_type: str = "stock",
) -> str:
    base0 = [
        "u.*", "f.first_date",
        "lag(close) OVER stock_window AS prev_close",
        "lag(volume) OVER stock_window AS prev_volume",
    ]
    base0 += [f"lag(close, {n}) OVER stock_window AS lag_close_{n}" for n in req["momentum"]]
    ma_periods = sorted(set(req["ma_periods"]) | set(req["rolling"]))
    base0 += [
        f"avg(close) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS ma_{n}"
        for n in ma_periods
    ]
    base0 += [
        f"avg(coalesce(volume, 0)) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS vma_{n}"
        for n in req["rolling"]
    ]
    base0 += [
        f"max(close) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS max_close_{n}, "
        f"min(close) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS min_close_{n}, "
        f"max(high) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS max_high_{n}, "
        f"min(low) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS min_low_{n}, "
        f"stddev_samp(close) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS std_close_{n}, "
        f"stddev_samp(amount) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS std_amount_{n}"
        for n in req["rolling"]
    ]
    base0 += [
        f"avg(coalesce(amount, 0)) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS adv_{n}"
        for n in req["liquidities"]
    ]
    base0 += [
        f"max(close) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS peak_{n}"
        for n in req["drawdowns"]
    ]
    base0 += [
        f"max(high) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n} PRECEDING AND 1 PRECEDING) AS prior_high_{n}"
        for n in req["turtles"]
    ]
    base1 = [
        "b0.*", "close - prev_close AS delta",
        "close / nullif(prev_close, 0) - 1 AS daily_return",
        "volume / nullif(prev_volume, 0) - 1 AS volume_change",
        "(high + low + close) / 3.0 AS typical_price",
        "lag((high + low + close) / 3.0) OVER stock_window AS prev_typical_price",
        "greatest(high-low, abs(high-prev_close), abs(low-prev_close)) AS true_range",
        "((close-low)-(high-close))/nullif(high-low,0)*coalesce(volume,0) AS clv_volume",
        "abs(close/nullif(prev_close,0)-1)/nullif(coalesce(amount,0),0) AS illiquidity",
        "date_diff('day', first_date, date) AS listing_days",
    ]
    base1 += [f"lag(vma_{n}) OVER stock_window AS prev_vma_{n}" for n in req["rolling"]]
    base1 += [
        f"lag(ma_{period}, {lag_days}) OVER stock_window AS ma_{period}_lag_{lag_days}"
        for period, lag_days in req["ma_lags"]
    ]
    base2 = [
        "b1.*",
        "CASE WHEN typical_price>prev_typical_price THEN typical_price*coalesce(volume,0) ELSE 0 END AS positive_money_flow",
        "CASE WHEN typical_price<prev_typical_price THEN typical_price*coalesce(volume,0) ELSE 0 END AS negative_money_flow",
    ]
    for n in req["rsis"]:
        base2.extend([
            f"avg(greatest(delta, 0)) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS gain_{n}",
            f"avg(greatest(-delta, 0)) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS loss_{n}",
        ])
    base2 += [
        f"stddev_samp(daily_return) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS vol_{n}"
        for n in req["rolling"]
    ]
    base2 += [
        f"avg(true_range) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS atr_{n}, "
        f"avg((high-low)/nullif(prev_close,0)) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS avg_range_{n}, "
        f"sqrt(avg(CASE WHEN daily_return<0 THEN daily_return*daily_return ELSE 0 END) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW)) AS downside_vol_{n}, "
        f"sum(abs(delta)) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS path_length_{n}, "
        f"sum(CASE WHEN typical_price>prev_typical_price THEN typical_price*coalesce(volume,0) ELSE 0 END) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS positive_mf_{n}, "
        f"sum(CASE WHEN typical_price<prev_typical_price THEN typical_price*coalesce(volume,0) ELSE 0 END) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS negative_mf_{n}, "
        f"sum(clv_volume) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW)/nullif(sum(coalesce(volume,0)) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW),0) AS cmf_{n}, "
        f"avg(clv_volume) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW)/nullif(vma_{n},0) AS ad_strength_{n}, "
        f"avg(illiquidity) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS amihud_{n}, "
        f"corr(daily_return, volume_change) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS pv_corr_{n}, "
        f"avg(typical_price) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS tp_ma_{n}"
        for n in req["rolling"]
    ]
    base3 = ["b2.*"]
    for n in req["rsis"]:
        base3.append(
            f"CASE WHEN loss_{n}=0 AND gain_{n}>0 THEN 100.0 "
            f"WHEN loss_{n}=0 THEN 50.0 "
            f"ELSE 100.0 - 100.0/(1.0 + gain_{n}/loss_{n}) END AS rsi_{n}"
        )
    base3 += [
        f"CASE WHEN negative_mf_{n}=0 AND positive_mf_{n}>0 THEN 100.0 "
        f"WHEN negative_mf_{n}=0 THEN 50.0 ELSE 100.0-100.0/(1.0+positive_mf_{n}/negative_mf_{n}) END AS mfi_{n}, "
        f"(typical_price-tp_ma_{n})/nullif(0.015*avg(abs(typical_price-tp_ma_{n})) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW),0) AS cci_{n}"
        for n in req["rolling"]
    ]
    if calendar_scope_clause and calendar_scope_clause != scope_clause:
        daily_counts_sql = f"""
        SELECT d.date, count(DISTINCT d.instrument_id) AS stock_count
        FROM daily_bar d JOIN instrument_master m USING (instrument_id)
        WHERE m.asset_type='stock' AND {calendar_scope_clause}
          AND d.date BETWEEN (SELECT min(date) FROM universe_raw)
                         AND (SELECT max(date) FROM universe_raw)
          AND d.open>0 AND d.high>0 AND d.low>0 AND d.close>0
        GROUP BY d.date
        """
    else:
        daily_counts_sql = """
        SELECT date, count(DISTINCT instrument_id) AS stock_count
        FROM universe_raw GROUP BY date
        """
    return f"""
    WITH selected_catalog AS (
        SELECT m.instrument_id, m.code, m.name, m.exchange
        FROM instrument_master m
        WHERE {_asset_clause(asset_type)}
          AND regexp_matches(coalesce(m.code, ''), '^[0-9]{{6}}$')
          AND {scope_clause}
    ),
    first_dates AS (
        SELECT d.instrument_id, min(d.date) AS first_date
        FROM daily_bar d JOIN selected_catalog c USING (instrument_id)
        GROUP BY d.instrument_id
    ),
    universe_raw AS (
        SELECT d.date, d.instrument_id, c.code, c.name, c.exchange,
               d.open, d.high, d.low, d.close, d.volume, d.amount
        FROM daily_bar d JOIN selected_catalog c USING (instrument_id)
        WHERE d.date BETWEEN ? AND ?
          AND d.open>0 AND d.high>0 AND d.low>0 AND d.close>0
    ),
    daily_counts AS (
        {daily_counts_sql}
    ),
    coverage AS (
        SELECT date, stock_count,
               median(stock_count) OVER (ORDER BY date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS prior_20_median
        FROM daily_counts
    ),
    complete_dates AS (
        SELECT date FROM coverage
        WHERE prior_20_median IS NULL OR stock_count >= greatest(1, prior_20_median*0.80)
    ),
    base0 AS (
        SELECT {', '.join(base0)}
        FROM universe_raw u
        JOIN complete_dates USING (date)
        JOIN first_dates f USING (instrument_id)
        WINDOW stock_window AS (PARTITION BY instrument_id ORDER BY date)
    ),
    base1 AS (
        SELECT {', '.join(base1)} FROM base0 b0
        WINDOW stock_window AS (PARTITION BY instrument_id ORDER BY date)
    ),
    base2 AS (
        SELECT {', '.join(base2)} FROM base1 b1
        WINDOW stock_window AS (PARTITION BY instrument_id ORDER BY date)
    ), base3 AS (
        SELECT {', '.join(base3)} FROM base2 b2
    )
    SELECT * FROM base3
    """


def _attach_recursive_indicators(
    connection: duckdb.DuckDBPyConnection, req: dict[str, Any]
) -> None:
    """Attach exact chronological EMA/MACD/KDJ/OBV/brick values.

    DuckDB rolling windows cover most factors.  Indicators whose definitions
    are recursive are calculated here per instrument, in date order, so their
    values cannot read a later bar.
    """
    ema_periods = sorted(set(req.get("ema_periods") or []) | {12, 26})
    ema_columns = [f"ema_{period}" for period in ema_periods]
    recursive_columns = [
        "instrument_id", "date", "indicator_rn", *ema_columns,
        "macd_dea", "kdj_k", "kdj_d", "brick_var2", "brick_var4",
        "brick_var5", "obv_value",
    ]
    anchor_emas = ["close::DOUBLE" for _ in ema_periods]
    next_emas = {
        period: f"({2.0/(period+1):.17g}*o.close+{1.0-2.0/(period+1):.17g}*r.ema_{period})"
        for period in ema_periods
    }
    next_ema_values = [next_emas[period] for period in ema_periods]
    next_dif = f"({next_emas[12]}-{next_emas[26]})"
    obv_lags = ", ".join(
        f"lag(obv_value, {window}) OVER indicator_window AS obv_lag_{window}"
        for window in req.get("obv_windows") or []
    )
    obv_lag_select = f", {obv_lags}" if obv_lags else ""
    ema_select = ", ".join(f"r.ema_{period}" for period in ema_periods)
    connection.execute(f"""
        CREATE TEMP TABLE lego_recursive_indicators AS
        WITH RECURSIVE ordered AS (
            SELECT f.*,
                   row_number() OVER (PARTITION BY instrument_id ORDER BY date) AS indicator_rn,
                   min(low) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN 8 PRECEDING AND CURRENT ROW) AS low9,
                   max(high) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN 8 PRECEDING AND CURRENT ROW) AS high9,
                   min(low) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN 3 PRECEDING AND CURRENT ROW) AS low4,
                   max(high) OVER (PARTITION BY instrument_id ORDER BY date ROWS BETWEEN 3 PRECEDING AND CURRENT ROW) AS high4
            FROM lego_features f
        ), inputs AS (
            SELECT o.*,
                   CASE WHEN indicator_rn<9 OR high9=low9 THEN 50.0
                        ELSE 100.0*(close-low9)/nullif(high9-low9,0) END AS kdj_rsv,
                   CASE WHEN indicator_rn<4 THEN NULL
                        WHEN high4=low4 THEN -40.0
                        ELSE 100.0*(high4-close)/nullif(high4-low4,0)-90.0 END AS brick_raw2,
                   CASE WHEN indicator_rn<4 THEN NULL
                        WHEN high4=low4 THEN 50.0
                        ELSE 100.0*(close-low4)/nullif(high4-low4,0) END AS brick_raw3
            FROM ordered o
        ), recursive_values({', '.join(recursive_columns)}) AS (
            SELECT instrument_id, date, indicator_rn,
                   {', '.join(anchor_emas)},
                   0.0::DOUBLE, 50.0::DOUBLE, 50.0::DOUBLE,
                   NULL::DOUBLE, NULL::DOUBLE, NULL::DOUBLE, 0.0::DOUBLE
            FROM inputs WHERE indicator_rn=1
            UNION ALL
            SELECT o.instrument_id, o.date, o.indicator_rn,
                   {', '.join(next_ema_values)},
                   (0.2*{next_dif}+0.8*r.macd_dea),
                   CASE WHEN o.indicator_rn<9 THEN 50.0 ELSE (2.0*r.kdj_k+o.kdj_rsv)/3.0 END,
                   CASE WHEN o.indicator_rn<9 THEN 50.0 ELSE (2.0*r.kdj_d+(2.0*r.kdj_k+o.kdj_rsv)/3.0)/3.0 END,
                   CASE WHEN o.indicator_rn<4 THEN NULL WHEN r.brick_var2 IS NULL THEN o.brick_raw2
                        ELSE (o.brick_raw2+3.0*r.brick_var2)/4.0 END,
                   CASE WHEN o.indicator_rn<4 THEN NULL WHEN r.brick_var4 IS NULL THEN o.brick_raw3
                        ELSE (o.brick_raw3+5.0*r.brick_var4)/6.0 END,
                   CASE WHEN o.indicator_rn<4 THEN NULL
                        WHEN r.brick_var5 IS NULL THEN CASE WHEN r.brick_var4 IS NULL THEN o.brick_raw3 ELSE (o.brick_raw3+5.0*r.brick_var4)/6.0 END
                        ELSE (CASE WHEN r.brick_var4 IS NULL THEN o.brick_raw3 ELSE (o.brick_raw3+5.0*r.brick_var4)/6.0 END+5.0*r.brick_var5)/6.0 END,
                   r.obv_value + CASE WHEN o.close>o.prev_close THEN coalesce(o.volume,0)
                                      WHEN o.close<o.prev_close THEN -coalesce(o.volume,0) ELSE 0 END
            FROM recursive_values r
            JOIN inputs o ON o.instrument_id=r.instrument_id AND o.indicator_rn=r.indicator_rn+1
        ), indicator_values AS (
            SELECT r.instrument_id, r.date, {ema_select},
                   r.ema_12-r.ema_26 AS macd_dif, r.macd_dea,
                   2.0*((r.ema_12-r.ema_26)-r.macd_dea) AS macd_histogram,
                   r.kdj_k, r.kdj_d, 3.0*r.kdj_k-2.0*r.kdj_d AS kdj_j,
                   CASE WHEN r.brick_var2 IS NULL OR r.brick_var5 IS NULL THEN NULL
                        ELSE greatest(r.brick_var5-r.brick_var2-4.0,0.0) END AS brick_value,
                   r.obv_value
            FROM recursive_values r
        ), indicator_state AS (
            SELECT i.*,
                   lag(macd_dif) OVER indicator_window AS prev_macd_dif,
                   lag(macd_dea) OVER indicator_window AS prev_macd_dea,
                   lag(brick_value) OVER indicator_window AS prev_brick_value
                   {obv_lag_select}
            FROM indicator_values i
            WINDOW indicator_window AS (PARTITION BY instrument_id ORDER BY date)
        )
        SELECT * EXCLUDE(prev_brick_value),
               CASE WHEN brick_value IS NULL OR prev_brick_value IS NULL THEN 0
                    WHEN brick_value>prev_brick_value+1e-12 THEN 1
                    WHEN brick_value<prev_brick_value-1e-12 THEN -1 ELSE 0 END AS brick_state
        FROM indicator_state
    """)
    connection.execute("""
        CREATE TEMP TABLE lego_features_with_recursive AS
        SELECT f.*, r.* EXCLUDE(instrument_id,date)
        FROM lego_features f
        LEFT JOIN lego_recursive_indicators r USING (instrument_id,date)
    """)
    connection.execute("DROP TABLE lego_features")
    connection.execute("ALTER TABLE lego_features_with_recursive RENAME TO lego_features")


def _attach_zettaranc_features(connection: duckdb.DuckDBPyConnection) -> None:
    """Attach exact recursive daily indicators used by the strict B1 block.

    KDJ(9,3,3), both EMA layers and MACD are evaluated in chronological
    order for every instrument.  The query never references a later row and
    joins one feature row back to each existing market row.
    """
    connection.execute("""
        CREATE TEMP TABLE lego_features_zettaranc AS
        WITH RECURSIVE ordered AS (
            SELECT f.*,
                   row_number() OVER (
                       PARTITION BY instrument_id ORDER BY date
                   ) AS z_rn,
                   min(low) OVER (
                       PARTITION BY instrument_id ORDER BY date
                       ROWS BETWEEN 8 PRECEDING AND CURRENT ROW
                   ) AS z_low9,
                   max(high) OVER (
                       PARTITION BY instrument_id ORDER BY date
                       ROWS BETWEEN 8 PRECEDING AND CURRENT ROW
                   ) AS z_high9,
                   min(low) OVER (
                       PARTITION BY instrument_id ORDER BY date
                       ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
                   ) AS z_low4,
                   max(high) OVER (
                       PARTITION BY instrument_id ORDER BY date
                       ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
                   ) AS z_high4,
                   min(low) OVER (
                       PARTITION BY instrument_id ORDER BY date
                       ROWS BETWEEN 9 PRECEDING AND CURRENT ROW
                   ) AS z_recent_low10,
                   min(low) OVER (
                       PARTITION BY instrument_id ORDER BY date
                       ROWS BETWEEN 19 PRECEDING AND 10 PRECEDING
                   ) AS z_previous_low10
            FROM lego_features f
        ), ordered_values AS (
            SELECT o.*,
                   CASE WHEN z_rn<9 THEN 50.0
                        WHEN z_high9=z_low9 THEN 50.0
                        ELSE 100.0*(close-z_low9)/nullif(z_high9-z_low9,0)
                   END AS z_rsv,
                   CASE WHEN z_rn<4 THEN NULL
                        WHEN z_high4=z_low4 THEN -40.0
                        ELSE 100.0*(z_high4-close)/nullif(z_high4-z_low4,0)-90.0
                   END AS z_brick_raw2,
                   CASE WHEN z_rn<4 THEN NULL
                        WHEN z_high4=z_low4 THEN 50.0
                        ELSE 100.0*(close-z_low4)/nullif(z_high4-z_low4,0)
                   END AS z_brick_raw3
            FROM ordered o
        ), recursive_values(
            instrument_id, date, z_rn,
            z_ema10, z_white, z_ema12, z_ema26,
            z_k, z_d, z_var2, z_var4, z_var5
        ) AS (
            SELECT instrument_id, date, z_rn,
                   close::DOUBLE, close::DOUBLE, close::DOUBLE, close::DOUBLE,
                   50.0::DOUBLE, 50.0::DOUBLE,
                   NULL::DOUBLE, NULL::DOUBLE, NULL::DOUBLE
            FROM ordered_values WHERE z_rn=1
            UNION ALL
            SELECT o.instrument_id, o.date, o.z_rn,
                   (2.0*o.close+9.0*r.z_ema10)/11.0,
                   (2.0*((2.0*o.close+9.0*r.z_ema10)/11.0)+9.0*r.z_white)/11.0,
                   (2.0*o.close+11.0*r.z_ema12)/13.0,
                   (2.0*o.close+25.0*r.z_ema26)/27.0,
                   CASE WHEN o.z_rn<9 THEN 50.0
                        ELSE (2.0*r.z_k+o.z_rsv)/3.0 END,
                   CASE WHEN o.z_rn<9 THEN 50.0
                        ELSE (2.0*r.z_d+(2.0*r.z_k+o.z_rsv)/3.0)/3.0 END,
                   CASE WHEN o.z_rn<4 THEN NULL
                        WHEN r.z_var2 IS NULL THEN o.z_brick_raw2
                        ELSE (o.z_brick_raw2+3.0*r.z_var2)/4.0 END,
                   CASE WHEN o.z_rn<4 THEN NULL
                        WHEN r.z_var4 IS NULL THEN o.z_brick_raw3
                        ELSE (o.z_brick_raw3+5.0*r.z_var4)/6.0 END,
                   CASE WHEN o.z_rn<4 THEN NULL
                        WHEN r.z_var5 IS NULL THEN
                            CASE WHEN r.z_var4 IS NULL THEN o.z_brick_raw3
                                 ELSE (o.z_brick_raw3+5.0*r.z_var4)/6.0 END
                        ELSE (
                            CASE WHEN r.z_var4 IS NULL THEN o.z_brick_raw3
                                 ELSE (o.z_brick_raw3+5.0*r.z_var4)/6.0 END
                            +5.0*r.z_var5
                        )/6.0 END
            FROM recursive_values r
            JOIN ordered_values o
              ON o.instrument_id=r.instrument_id AND o.z_rn=r.z_rn+1
        ), indicator_values AS (
            SELECT o.instrument_id, o.date,
                   r.z_k AS z_kdj_k, r.z_d AS z_kdj_d,
                   3.0*r.z_k-2.0*r.z_d AS z_kdj_j,
                   r.z_white,
                   (o.ma_14+o.ma_28+o.ma_57+o.ma_114)/4.0 AS z_yellow,
                   r.z_ema12-r.z_ema26 AS z_macd_dif,
                   CASE WHEN r.z_var2 IS NULL OR r.z_var5 IS NULL THEN NULL
                        ELSE greatest(r.z_var5-r.z_var2-4.0,0.0) END AS z_brick,
                   o.z_recent_low10, o.z_previous_low10
            FROM recursive_values r
            JOIN ordered_values o USING (instrument_id,date)
        ), indicator_state AS (
            SELECT i.*,
                   lag(z_brick) OVER (
                       PARTITION BY instrument_id ORDER BY date
                   ) AS z_previous_brick,
                   lag(z_white) OVER (
                       PARTITION BY instrument_id ORDER BY date
                   ) AS z_previous_white,
                   lag(z_yellow) OVER (
                       PARTITION BY instrument_id ORDER BY date
                   ) AS z_previous_yellow,
                   lag(z_kdj_j) OVER (
                       PARTITION BY instrument_id ORDER BY date
                   ) AS z_previous_kdj_j
            FROM indicator_values i
        ), feature_rows AS (
            SELECT f.*,
                   s.z_kdj_k, s.z_kdj_d, s.z_kdj_j, s.z_previous_kdj_j,
                   s.z_white, s.z_yellow, s.z_previous_white, s.z_previous_yellow,
                   s.z_macd_dif, s.z_brick,
                   CASE WHEN s.z_brick IS NULL OR s.z_previous_brick IS NULL THEN 0
                        WHEN s.z_brick>s.z_previous_brick+1e-12 THEN 1
                        WHEN s.z_brick<s.z_previous_brick-1e-12 THEN -1 ELSE 0 END
                        AS z_brick_state,
                   s.z_recent_low10, s.z_previous_low10,
                   coalesce(s.z_recent_low10>=s.z_previous_low10*0.98,FALSE) AS z_n_lift,
                   f.volume/nullif(f.prev_volume,0) AS z_volume_ratio
            FROM lego_features f
            LEFT JOIN indicator_state s USING (instrument_id,date)
        ), standard_b1 AS (
            SELECT p.*,
                   coalesce(
                       z_kdj_j<=-10.0
                       AND daily_return*100.0 BETWEEN -2.0 AND 1.8
                       AND 100.0*(high-low)/nullif(prev_close,0) <
                           CASE WHEN exchange='BJ' OR code LIKE '300%' OR code LIKE '301%'
                                     OR code LIKE '688%' THEN 7.0 ELSE 4.0 END
                       AND z_volume_ratio<=0.8
                       AND z_n_lift
                       AND coalesce(z_brick_state,0)>=0
                       AND z_white>=z_yellow
                       AND (close>=ma_60 OR close>=z_yellow)
                       AND z_macd_dif>=0
                       AND NOT(close<open AND daily_return<=-0.03 AND z_volume_ratio>=1.5),
                       FALSE
                   ) AS z_b1_standard,
                   coalesce(
                       (close<open AND daily_return<=-0.03 AND z_volume_ratio>=1.5)
                       OR z_brick_state<0
                       OR (z_previous_white>=z_previous_yellow AND z_white<z_yellow)
                       OR close<z_yellow,
                       FALSE
                   ) AS z_exit_risk_standard,
                   CASE
                       WHEN close<open AND daily_return<=-0.03 AND z_volume_ratio>=1.5 THEN 'S1放量大阴'
                       WHEN z_brick_state<0 THEN '红砖翻绿'
                       WHEN z_previous_white>=z_previous_yellow AND z_white<z_yellow THEN '白线死叉黄线'
                       WHEN close<z_yellow THEN '跌破黄线'
                       ELSE ''
                   END AS z_exit_reason,
                   CASE WHEN z_white>=z_yellow THEN '白线≥黄线' ELSE '白线<黄线' END AS z_line_state,
                   CASE WHEN z_brick_state<0 THEN '绿砖'
                        WHEN z_brick_state>0 THEN '红砖' ELSE '中性' END AS z_brick_color
            FROM feature_rows p
        ), standard_b2 AS (
            SELECT b.*,
                   coalesce(
                       (lag(z_b1_standard,1,FALSE) OVER z_stock_b2
                        OR lag(z_b1_standard,2,FALSE) OVER z_stock_b2
                        OR lag(z_b1_standard,3,FALSE) OVER z_stock_b2)
                       AND daily_return>=0.04
                       AND z_volume_ratio>=1.2
                       AND z_kdj_j<55.0
                       AND z_kdj_j>z_previous_kdj_j
                       AND z_white>=z_yellow
                       AND (close>=ma_60 OR close>=z_yellow)
                       AND z_macd_dif>=0
                       AND NOT z_exit_risk_standard,
                       FALSE
                   ) AS z_b2_standard
            FROM standard_b1 b
            WINDOW z_stock_b2 AS (PARTITION BY instrument_id ORDER BY date)
        ), standard_b3 AS (
            SELECT q.*,
                   coalesce(
                       (lag(z_b2_standard,1,FALSE) OVER z_stock_b3
                        OR lag(z_b2_standard,2,FALSE) OVER z_stock_b3)
                       AND daily_return>0.0 AND daily_return<0.02
                       AND 100.0*(high-low)/nullif(prev_close,0)<7.0
                       AND z_volume_ratio<=0.7
                       AND z_white>=z_yellow
                       AND (close>=ma_60 OR close>=z_yellow)
                       AND z_macd_dif>=0
                       AND NOT z_exit_risk_standard,
                       FALSE
                   ) AS z_b3_standard
            FROM standard_b2 q
            WINDOW z_stock_b3 AS (PARTITION BY instrument_id ORDER BY date)
        )
        SELECT s.*,
               CASE WHEN z_b3_standard THEN 'Z·B3'
                    WHEN z_b2_standard THEN 'Z·B2'
                    WHEN z_b1_standard THEN 'Z·B1' ELSE '' END AS z_signal,
               (CASE WHEN z_b3_standard THEN 1.00
                     WHEN z_b2_standard THEN 0.78
                     WHEN z_b1_standard THEN 0.52 ELSE 0.0 END)
               +0.15*least(1.0,greatest(0.0,(z_white/nullif(z_yellow,0)-1.0)/0.08))
               +0.10*least(1.0,greatest(0.0,z_macd_dif/nullif(close,0)*100.0))
               +0.05*CASE WHEN coalesce(z_brick_state,0)>=0 THEN 1.0 ELSE 0.0 END
               AS z_resonance_score
        FROM standard_b3 s
    """)
    before, after = connection.execute(
        "SELECT (SELECT count(*) FROM lego_features), "
        "(SELECT count(*) FROM lego_features_zettaranc)"
    ).fetchone()
    if int(before or 0) != int(after or 0):
        raise ValueError("Z·递归指标连接后行数发生变化，已拒绝继续回测")
    connection.execute("DROP TABLE lego_features")
    connection.execute("ALTER TABLE lego_features_zettaranc RENAME TO lego_features")


def _zettaranc_b1_condition(block: BlockSpec) -> str:
    params = block.params
    amplitude_limit = (
        f"CASE WHEN exchange='BJ' OR code LIKE '300%' OR code LIKE '301%' "
        f"OR code LIKE '688%' THEN {float(params.get('growth_max_amplitude', 7.0))} "
        f"ELSE {float(params.get('main_max_amplitude', 4.0))} END"
    )
    conditions = [
        f"z_kdj_j<={float(params.get('j_threshold', -10.0))}",
        f"daily_return*100.0 BETWEEN {float(params.get('min_gain', -2.0))} "
        f"AND {float(params.get('max_gain', 1.8))}",
        f"100.0*(high-low)/nullif(prev_close,0)<({amplitude_limit})",
        f"z_volume_ratio<={float(params.get('max_volume_ratio', 0.8))}",
        f"z_recent_low10>=z_previous_low10*{float(params.get('n_lift_floor', 0.98))}",
    ]
    if bool(params.get("require_non_green_brick", True)):
        conditions.append("coalesce(z_brick_state,0)>=0")
    if bool(params.get("require_white_above_yellow", True)):
        conditions.append("z_white>=z_yellow")
    if bool(params.get("require_support", True)):
        conditions.append("(close>=ma_60 OR close>=z_yellow)")
    if bool(params.get("require_macd_positive", True)):
        conditions.append("z_macd_dif>=0")
    if bool(params.get("require_no_s1", True)):
        conditions.append(
            "NOT(close<open AND daily_return<=-0.03 AND z_volume_ratio>=1.5)"
        )
    return "(" + " AND ".join(conditions) + ")"


def _z_b123_condition(block: BlockSpec) -> str:
    stages = []
    if bool(block.params.get("allow_b1", True)):
        stages.append("coalesce(z_b1_standard,FALSE)")
    if bool(block.params.get("allow_b2", True)):
        stages.append("coalesce(z_b2_standard,FALSE)")
    if bool(block.params.get("allow_b3", True)):
        stages.append("coalesce(z_b3_standard,FALSE)")
    if not stages:
        raise ValueError("Z·B1/B2/B3 三阶段准入至少启用一个阶段")
    return "(" + " OR ".join(stages) + ")"


def _z_exit_risk_guard_condition(block: BlockSpec) -> str:
    params = block.params
    risks = []
    if bool(params.get("forbid_s1", True)):
        risks.append("(close<open AND daily_return<=-0.03 AND z_volume_ratio>=1.5)")
    if bool(params.get("forbid_green_flip", True)):
        risks.append("coalesce(z_brick_state,0)<0")
    if bool(params.get("forbid_white_dead_cross", True)):
        risks.append("(z_previous_white>=z_previous_yellow AND z_white<z_yellow)")
    if bool(params.get("forbid_below_yellow", True)):
        risks.append("close<z_yellow")
    return "TRUE" if not risks else "NOT(" + " OR ".join(risks) + ")"


def _filter_expression(block: BlockSpec) -> str:
    params = block.params
    if block.type == "filter_non_st":
        return "upper(coalesce(name, '')) NOT LIKE '%ST%'"
    if block.type == "filter_tradable":
        return "volume>0 AND coalesce(amount, 0)>0 AND open>0 AND high>0 AND low>0 AND close>0"
    if block.type == "filter_not_limit_up":
        rate = (
            "CASE WHEN upper(coalesce(name,'')) LIKE '%ST%' THEN 0.05 "
            "WHEN exchange='BJ' OR code LIKE '4%' OR code LIKE '8%' OR code LIKE '92%' THEN 0.30 "
            "WHEN code LIKE '300%' OR code LIKE '301%' OR code LIKE '688%' THEN 0.20 ELSE 0.10 END"
        )
        return f"prev_close>0 AND close < round(prev_close*(1+({rate})), 2)-0.001"
    if block.type == "filter_listing_days":
        return f"listing_days >= {float(params.get('min_days', 120))}"
    if block.type == "filter_adv20":
        window = _int_param(block, "window", 20)
        return f"adv_{window} >= {float(params.get('min_amount', 20_000_000.0))}"
    if block.type == "filter_custom_formula":
        expression, _ = _compile_custom_formula(str(params.get("formula") or ""), boolean=True)
        return expression
    if block.type in {"filter_active_market_gate", "filter_ths_active_market_proxy_gate"}:
        return "coalesce(active_market_gate, FALSE)"
    if block.type == "strategy_momentum":
        lookback = _int_param(block, "lookback", 20)
        return f"close/nullif(lag_close_{lookback},0)-1 >= {float(params.get('min_return', 0.0))}"
    if block.type == "strategy_ma_cross":
        fast = _int_param(block, "fast", 5)
        slow = max(fast + 1, _int_param(block, "slow", 20))
        return f"ma_{fast} > ma_{slow}"
    if block.type == "strategy_rsi":
        period = _int_param(block, "period", 14, 2)
        return f"rsi_{period} BETWEEN {float(params.get('min_value', 0.0))} AND {float(params.get('max_value', 70.0))}"
    if block.type == "strategy_turtle":
        entry = _int_param(block, "entry", 20, 2)
        return f"close > prior_high_{entry}"
    if block.type == "strategy_zettaranc_b1":
        return _zettaranc_b1_condition(block)
    if block.type == "strategy_z_b123":
        return _z_b123_condition(block)
    if block.type == "strategy_z_b1_stage":
        return "coalesce(z_b1_standard,FALSE)"
    if block.type == "strategy_z_b2_stage":
        return "coalesce(z_b2_standard,FALSE)"
    if block.type == "strategy_z_b3_stage":
        return "coalesce(z_b3_standard,FALSE)"
    if block.type == "strategy_z_exit_risk_guard":
        return _z_exit_risk_guard_condition(block)
    if block.type == "strategy_active_market_gate":
        return "coalesce(active_market_gate, FALSE)"
    if block.type == "custom_formula":
        expression, _ = _compile_custom_formula(str(params.get("formula") or ""), boolean=True)
        return expression
    raise ValueError(f"积木 {block.type} 不是可执行筛选块")


def _filter_groups(spec: PipelineSpec) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for block in [item for item in _enabled(spec) if item.category in {"filter", "strategy"}]:
        key = (block.category, str(block.params.get("group") or block.id))
        expression = _filter_expression(block)
        if not groups or groups[-1]["key"] != key:
            groups.append({
                "key": key, "category": block.category, "group": key[1],
                "name": block.name, "blocks": [block], "expression": f"({expression})",
            })
        else:
            current = groups[-1]
            current["expression"] = f"({current['expression']} {block.combine} ({expression}))"
            current["blocks"].append(block)
            current["name"] = " / ".join(item.name for item in current["blocks"])
    return groups


def _raw_factor(block: BlockSpec) -> str:
    if block.type in FACTOR_REGISTRY:
        kind = block.type
        p = block.params
        n = _int_param(block, "window", _int_param(block, "period", _int_param(block, "lookback", 20, 1), 1), 1)
        if kind == "factor_momentum": return f"close/nullif(lag_close_{_int_param(block, 'lookback', 20)},0)-1"
        if kind == "factor_multi_momentum":
            periods = [_int_param(block, key, default) for key, default in (("short", 5), ("medium", 20), ("long", 60))]
            return "(" + "+".join(f"close/nullif(lag_close_{period},0)-1" for period in periods) + ")/3.0"
        if kind == "factor_short_reversal": return f"-(close/nullif(lag_close_{_int_param(block, 'lookback', 5)},0)-1)"
        if kind == "factor_trend":
            fast, slow = _int_param(block, "fast", 5), _int_param(block, "slow", 20)
            return f"ma_{fast}/nullif(ma_{slow},0)-1"
        if kind in {"factor_ma_position", "factor_ma_deviation"}:
            period = _int_param(block, "period", 20)
            scale = "100.0*" if kind == "factor_ma_deviation" else ""
            return f"{scale}(close/nullif(ma_{period},0)-1)"
        if kind in {"factor_ma_cross_event", "factor_ma_breakdown_event"}:
            period = _int_param(block, "period", 20)
            if kind == "factor_ma_cross_event": return f"CASE WHEN close>ma_{period} AND prev_close<=ma_{period}_lag_1 THEN 1.0 ELSE 0.0 END"
            return f"CASE WHEN close<ma_{period} AND prev_close>=ma_{period}_lag_1 THEN 1.0 ELSE 0.0 END"
        if kind == "factor_ma_slope":
            period, lag_days = _int_param(block, "period", 20), _int_param(block, "slope_days", 5)
            return f"ma_{period}/nullif(ma_{period}_lag_{lag_days},0)-1"
        if kind == "factor_ma_alignment":
            fast, medium, slow = (_int_param(block, key, default) for key, default in (("fast", 5), ("medium", 10), ("slow", 20)))
            return f"ma_{fast}/nullif(ma_{medium},0)+ma_{medium}/nullif(ma_{slow},0)-2"
        if kind == "factor_ema_cross":
            fast, slow = _int_param(block, "fast", 12), _int_param(block, "slow", 26)
            return f"ema_{fast}/nullif(ema_{slow},0)-1"
        if kind == "factor_trend_efficiency": return f"abs(close-lag_close_{n})/nullif(path_length_{n},0)"
        if kind == "factor_donchian_position": return f"(close-min_low_{n})/nullif(max_high_{n}-min_low_{n},0)"
        if kind == "factor_macd_dif": return "macd_dif"
        if kind == "factor_macd_histogram": return "macd_histogram"
        if kind == "factor_macd_cross": return "CASE WHEN macd_dif>macd_dea AND prev_macd_dif<=prev_macd_dea THEN 1.0 ELSE 0.0 END"
        if kind == "factor_rsi": return f"rsi_{_int_param(block, 'period', 14, 2)}"
        if kind == "factor_kdj_k": return "kdj_k"
        if kind == "factor_kdj_j": return "kdj_j"
        if kind == "factor_cci": return f"cci_{_int_param(block, 'period', 14, 2)}"
        if kind == "factor_williams_r": return f"-100.0*(max_high_{n}-close)/nullif(max_high_{n}-min_low_{n},0)"
        if kind == "factor_high_52w_proximity": return f"close/nullif(max_close_{n},0)-1"
        if kind == "factor_low_volatility": return f"vol_{n}"
        if kind == "factor_atr": return f"atr_{n}"
        if kind == "factor_natr": return f"atr_{n}/nullif(close,0)"
        if kind == "factor_bollinger_position":
            width = float(p.get("stddev", 2.0)); return f"(close-(ma_{n}-{width}*std_close_{n}))/nullif({2*width}*std_close_{n},0)"
        if kind == "factor_bollinger_width":
            width = float(p.get("stddev", 2.0)); return f"{2*width}*std_close_{n}/nullif(ma_{n},0)"
        if kind == "factor_amplitude": return "(high-low)/nullif(prev_close,0)"
        if kind == "factor_downside_vol": return f"downside_vol_{n}"
        if kind == "factor_volatility_change":
            short, long = _int_param(block, "short", 10), _int_param(block, "long", 60)
            return f"vol_{short}/nullif(vol_{long},0)-1"
        if kind == "factor_drawdown": return f"close/nullif(max_close_{n},0)-1"
        if kind == "factor_liquidity": return f"adv_{n}"
        if kind == "factor_volume_ratio": return f"volume/nullif(vma_{n},0)"
        if kind == "factor_amount_ratio": return f"amount/nullif(adv_{n},0)"
        if kind == "factor_volume_ma_cross": return f"CASE WHEN volume>vma_{n} AND prev_volume<=prev_vma_{n} THEN 1.0 ELSE 0.0 END"
        if kind == "factor_obv_momentum": return f"(obv_value-obv_lag_{n})/nullif(vma_{n},0)"
        if kind == "factor_mfi": return f"mfi_{_int_param(block, 'period', 14, 2)}"
        if kind == "factor_cmf": return f"cmf_{n}"
        if kind == "factor_ad_slope": return f"ad_strength_{n}"
        if kind == "factor_price_volume_corr": return f"pv_corr_{n}"
        if kind == "factor_volume_contraction":
            short, long = _int_param(block, "short", 5), _int_param(block, "long", 20)
            return f"vma_{short}/nullif(vma_{long},0)-1"
        if kind == "factor_amihud": return f"amihud_{n}"
        if kind == "factor_amount_stability": return f"std_amount_{n}/nullif(adv_{n},0)"
        if kind == "factor_liquidity_shock": return f"amount/nullif(adv_{n},0)-1"
        if kind == "factor_intraday_strength": return "(close-low)/nullif(high-low,0)"
        if kind == "factor_overnight_return": return "open/nullif(prev_close,0)-1"
        if kind == "factor_body_ratio": return "abs(close-open)/nullif(high-low,0)"
        if kind == "factor_upper_shadow": return "(high-greatest(open,close))/nullif(high-low,0)"
        if kind == "factor_lower_shadow": return "(least(open,close)-low)/nullif(high-low,0)"
        if kind == "factor_range_compression": return f"((high-low)/nullif(prev_close,0))/nullif(avg_range_{n},0)"
        if kind == "factor_attention_proxy": return f"(volume/nullif(vma_{n},0))*abs(daily_return)"
        if kind == "factor_crowding_proxy": return f"abs(close/nullif(lag_close_{n},0)-1)*(volume/nullif(vma_{n},0))/nullif(vol_{n},0)"
        if kind == "factor_limit_up_proximity": return "daily_return/nullif(CASE WHEN name LIKE '%ST%' THEN 0.05 WHEN exchange='BJ' THEN 0.30 WHEN code LIKE '300%' OR code LIKE '301%' OR code LIKE '688%' THEN 0.20 ELSE 0.10 END,0)"
        if kind == "factor_brick": return "coalesce(brick_state,0)+0.001*coalesce(brick_value,0)"
        raise ValueError(f"注册因子 {kind} 缺少 SQL 实现")
    if block.type == "factor_custom_formula":
        expression, _ = _compile_custom_factor(str(block.params.get("formula") or ""))
        return expression
    if block.type == "penalty_custom_formula":
        expression, _ = _compile_custom_factor(str(block.params.get("formula") or ""))
        return expression
    if block.type == "penalty_volatility":
        return f"vol_{_int_param(block, 'window', 20, 2)}"
    if block.type == "penalty_drawdown":
        n = _int_param(block, "window", 60, 2)
        return f"-(close/nullif(peak_{n},0)-1)"
    if block.type == "penalty_liquidity":
        return f"-adv_{_int_param(block, 'window', 20)}"
    raise ValueError(f"积木 {block.type} 没有可执行因子表达式")


def _score_sql(spec: PipelineSpec, eligible_predicate: str) -> tuple[str, list[dict[str, Any]]]:
    blocks = _enabled(spec, "factor") + _enabled(spec, "penalty")
    normalizer = _one(spec, "normalize")
    sigmoid_k = float(normalizer.params.get("sigmoid_k", 1.0))
    clip = float(normalizer.params.get("clip", 6.0))
    descriptors: list[dict[str, Any]] = []
    raw_fields = []
    stats_fields = []
    normalized_fields = []
    for index, block in enumerate(blocks):
        alias = f"b{index}"
        raw_alias = f"{alias}_raw"
        score_alias = f"{alias}_score"
        raw_fields.append(f"({_raw_factor(block)})::DOUBLE AS {raw_alias}")
        method = str(block.params.get("method", "percentile" if block.category == "penalty" else "robust_sigmoid"))
        direction = str(block.params.get("direction", "higher"))
        if block.category == "penalty":
            direction = "higher"  # raw penalty is constructed so higher is worse
        if method == "robust_sigmoid":
            stats_fields.extend([f"median({raw_alias}) AS {alias}_median", f"mad({raw_alias}) AS {alias}_mad"])
            sign = 1.0 if direction == "higher" else -1.0
            normalized = (
                f"CASE WHEN {raw_alias} IS NULL THEN 50.0 "
                f"WHEN coalesce(s.{alias}_mad,0)=0 THEN 50.0 ELSE "
                f"100.0/(1.0+exp(-greatest(-{clip}, least({clip}, "
                f"{sigmoid_k}*{sign}*({raw_alias}-s.{alias}_median)/nullif(1.4826*s.{alias}_mad,0))))) END AS {score_alias}"
            )
        else:
            order = "ASC" if direction == "higher" else "DESC"
            normalized = (
                f"CASE WHEN {raw_alias} IS NULL THEN 50.0 ELSE "
                f"100.0*percent_rank() OVER (PARTITION BY r.date ORDER BY {raw_alias} {order} NULLS FIRST) "
                f"END AS {score_alias}"
            )
        normalized_fields.append(normalized)
        descriptors.append({
            "block": block, "alias": alias, "raw": raw_alias, "score": score_alias,
            "weight": float(block.params.get("weight", 0.0)),
        })
    factor_weight = sum(item["weight"] for item in descriptors if item["block"].category == "factor")
    contribution_fields = []
    total_parts = []
    for item in descriptors:
        contribution = f"{item['alias']}_contribution"
        if item["block"].category == "factor":
            expression = f"{item['score']}*{item['weight']}/nullif({factor_weight},0)"
        else:
            expression = f"-{item['score']}*{item['weight']}"
        contribution_fields.append(f"({expression}) AS {contribution}")
        total_parts.append(f"({expression})")
        item["contribution"] = contribution
    stats_select = ", ".join(["date", *stats_fields]) if stats_fields else "date"
    stats_join = "LEFT JOIN factor_stats s USING (date)" if stats_fields else ""
    sql = f"""
    WITH eligible AS (
        SELECT * FROM lego_features
        WHERE date BETWEEN ? AND ? AND ({eligible_predicate})
    ),
    raw_values AS (
        SELECT e.*, {', '.join(raw_fields)} FROM eligible e
    ),
    factor_stats AS (
        SELECT {stats_select} FROM raw_values GROUP BY date
    ),
    normalized AS (
        SELECT r.*, {', '.join(normalized_fields)}
        FROM raw_values r {stats_join}
    ),
    scored AS (
        SELECT n.*, {', '.join(contribution_fields)},
               ({' + '.join(total_parts)})::DOUBLE AS final_score
        FROM normalized n
    )
    SELECT *, row_number() OVER (
        PARTITION BY date ORDER BY final_score DESC NULLS LAST, adv_20 DESC NULLS LAST, instrument_id
    ) AS candidate_rank
    FROM scored
    WHERE isfinite(final_score)
    """
    return sql, descriptors


def _count(connection: duckdb.DuckDBPyConnection, predicate: str, start: date, end: date) -> dict[str, int]:
    row = connection.execute(
        f"""SELECT count(*) AS rows, count(DISTINCT instrument_id) AS instruments,
                   count(DISTINCT date) AS dates
            FROM lego_features WHERE date BETWEEN ? AND ? AND ({predicate})""",
        [start, end],
    ).fetchone()
    return {"rows": int(row[0] or 0), "instruments": int(row[1] or 0), "dates": int(row[2] or 0)}


def _count_many(
    connection: duckdb.DuckDBPyConnection,
    predicates: list[str],
    start: date,
    end: date,
) -> list[dict[str, int]]:
    """Evaluate the entire filter funnel in one scan of the feature table."""
    fields: list[str] = []
    for index, predicate in enumerate(predicates):
        fields.extend([
            f"count(*) FILTER (WHERE ({predicate})) AS rows_{index}",
            f"count(DISTINCT instrument_id) FILTER (WHERE ({predicate})) AS instruments_{index}",
            f"count(DISTINCT date) FILTER (WHERE ({predicate})) AS dates_{index}",
        ])
    row = connection.execute(
        f"SELECT {', '.join(fields)} FROM lego_features WHERE date BETWEEN ? AND ?",
        [start, end],
    ).fetchone()
    return [
        {
            "rows": int(row[index * 3] or 0),
            "instruments": int(row[index * 3 + 1] or 0),
            "dates": int(row[index * 3 + 2] or 0),
        }
        for index in range(len(predicates))
    ]


def _active_market_gate_source(block: BlockSpec) -> tuple[str, Path]:
    """Resolve a gate provider to a fixed local file (never an arbitrary path)."""
    if block.type == "filter_ths_active_market_proxy_gate":
        expected_provider = "ths_internal_proxy"
        source = THS_ACTIVE_PROXY_FILE
    else:
        expected_provider = "compass"
        source = ACTIVE_VALUE_FILE
    provider = str(block.params.get("provider", expected_provider))
    if provider != expected_provider:
        raise ValueError(f"{block.name} 的 provider 必须是 {expected_provider}")
    return provider, source


def _read_active_market_gate_payload(block: BlockSpec) -> tuple[str, Path, dict[str, Any]]:
    provider, source = _active_market_gate_source(block)
    if not source.exists():
        raise FileNotFoundError(f"活跃市值历史不存在: {source}")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("活跃市值文件顶层必须是对象")
    if provider == "ths_internal_proxy":
        if payload.get("provider") != provider or payload.get("quality") != "close-only":
            raise ValueError("同花顺拟合文件缺少 provider=ths_internal_proxy 或 quality=close-only 标识")
        if payload.get("not_compass") is not True:
            raise ValueError("同花顺拟合文件必须明确标注 not_compass=true")
    return provider, source, payload


def _active_market_gate_frame(
    block: BlockSpec,
    payload: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Replay the selected active-market switch once for every historical date."""
    if payload is None:
        _, _, payload = _read_active_market_gate_payload(block)
    candles = payload.get("candles", [])
    frame = pd.DataFrame(candles)
    if frame.empty or not {"date", "close"}.issubset(frame.columns):
        raise ValueError("活跃市值文件缺少 candles.date/close")
    frame["gate_date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    frame["active_close"] = pd.to_numeric(frame["close"], errors="coerce")
    invalid = (
        frame["gate_date"].isna()
        | frame["active_close"].isna()
        | ~np.isfinite(frame["active_close"])
        | (frame["active_close"] <= 0)
    )
    if bool(invalid.any()):
        raise ValueError(f"活跃市值文件包含 {int(invalid.sum())} 条非法日期或非正/非有限收盘值")
    if bool(frame["gate_date"].duplicated(keep=False).any()):
        duplicated = frame.loc[
            frame["gate_date"].duplicated(keep=False), "gate_date"
        ].dt.date.astype(str).drop_duplicates().tolist()
        raise ValueError(f"活跃市值文件包含重复日期: {', '.join(duplicated[:5])}")
    frame = frame.sort_values("gate_date")
    ma_period = max(2, int(block.params.get("ma_period", 10)))
    start_one = float(block.params.get("start_one", 4.0))
    start_two = float(block.params.get("start_two", 5.0))
    use_two_day = bool(block.params.get("use_two_day", False))
    exit_one = float(block.params.get("exit_one", -2.33))
    ma_buffer = float(block.params.get("ma_buffer", 0.0))
    frame["active_one_day"] = frame["active_close"].pct_change() * 100.0
    frame["active_two_day"] = frame["active_close"].pct_change(2) * 100.0
    frame["active_ma"] = frame["active_close"].rolling(ma_period, min_periods=ma_period).mean()
    frame["active_ma_threshold"] = frame["active_ma"] * (1.0 + ma_buffer)
    enabled = False
    states: list[bool] = []
    events: list[str] = []
    reasons: list[str] = []
    for row in frame.itertuples(index=False):
        event = ""
        reason = ""
        one = float(row.active_one_day) if pd.notna(row.active_one_day) else 0.0
        two = float(row.active_two_day) if pd.notna(row.active_two_day) else 0.0
        moving_average = float(row.active_ma) if pd.notna(row.active_ma) else None
        ma_threshold = moving_average * (1.0 + ma_buffer) if moving_average is not None else None
        exit_by_loss = one <= exit_one
        exit_by_ma = ma_threshold is not None and float(row.active_close) < ma_threshold
        exit_trigger = exit_by_loss or exit_by_ma
        start_trigger = one >= start_one or (use_two_day and two >= start_two)
        # Exit conditions have priority. In particular, a rebound that reaches
        # the start threshold while still below the buffered MA must not create
        # a historical false opening.
        if enabled and exit_trigger:
            enabled = False
            event = "结束"
            exit_reasons = []
            if exit_by_loss:
                exit_reasons.append(f"单日涨幅≤{exit_one:g}%")
            if exit_by_ma:
                buffer_label = f"×{1.0 + ma_buffer:g}" if ma_buffer else ""
                exit_reasons.append(f"跌破MA{ma_period}{buffer_label}")
            reason = " 或 ".join(exit_reasons)
        elif not enabled and start_trigger and not exit_trigger:
            enabled = True
            event = "开启"
        states.append(enabled)
        events.append(event)
        reasons.append(reason)
    frame["active_market_gate"] = states
    frame["active_gate_event"] = events
    frame["active_gate_reason"] = reasons
    return frame[[
        "gate_date", "active_market_gate", "active_one_day", "active_two_day",
        "active_close", "active_ma", "active_ma_threshold",
        "active_gate_event", "active_gate_reason",
    ]].reset_index(drop=True)


def _attach_active_market_gate(
    connection: duckdb.DuckDBPyConnection,
    block: BlockSpec,
    start: date,
    end: date,
) -> dict[str, Any]:
    provider, source, payload = _read_active_market_gate_payload(block)
    if provider == "ths_internal_proxy":
        formal_start = pd.Timestamp(payload.get("formal_backtest_start", "2023-01-01"))
        if pd.Timestamp(start) < formal_start:
            raise ValueError(
                f"同花顺代理使用 2016—2022 校准参数；正式样本外回测不得早于 {formal_start.date()}"
            )
    gate = _active_market_gate_frame(block, payload)
    feature_bounds = connection.execute(
        "SELECT min(date), max(date) FROM lego_features WHERE date BETWEEN ? AND ?",
        [start, end],
    ).fetchone()
    gate_start = pd.Timestamp(gate["gate_date"].min())
    gate_end = pd.Timestamp(gate["gate_date"].max())
    if feature_bounds[0] is not None and pd.Timestamp(feature_bounds[0]) < gate_start:
        raise ValueError(
            f"活跃市值从 {gate_start.date()} 才有历史，不能覆盖当前特征预热起点 {feature_bounds[0]}"
        )
    max_gap_days = max(0, int(block.params.get("max_gap_days", 0)))
    connection.register("active_market_gate_frame", gate)
    stale_expression = (
        "g.gate_date IS NULL OR "
        f"date_diff('day', g.gate_date, f.date) > {max_gap_days}"
    )
    connection.execute(f"""
        CREATE TEMP TABLE lego_features_with_gate AS
        SELECT f.*, g.gate_date AS active_gate_source_date,
               g.active_market_gate AS active_gate_source_enabled,
               CASE WHEN {stale_expression} THEN FALSE
                    ELSE coalesce(g.active_market_gate, FALSE) END AS active_market_gate,
               g.active_one_day, g.active_two_day,
               g.active_close, g.active_ma, g.active_ma_threshold,
               CASE WHEN {stale_expression} AND coalesce(g.active_market_gate, FALSE)
                    THEN '结束' ELSE coalesce(g.active_gate_event, '') END AS active_gate_event,
               CASE WHEN {stale_expression}
                    THEN '活跃市值数据缺口，门控关闭（不沿用旧值）'
                    ELSE coalesce(g.active_gate_reason, '') END AS active_gate_reason
        FROM lego_features f
        ASOF LEFT JOIN active_market_gate_frame g ON f.date >= g.gate_date
    """)
    connection.execute("DROP TABLE lego_features")
    connection.execute("ALTER TABLE lego_features_with_gate RENAME TO lego_features")
    gap = connection.execute(
        "SELECT count(*) FILTER (WHERE active_gate_source_date IS NULL), "
        "max(date_diff('day', active_gate_source_date, date)) FROM lego_features "
        "WHERE date BETWEEN ? AND ?",
        [start, end],
    ).fetchone()
    gap_closed = connection.execute(
        f"""SELECT count(*), count(DISTINCT date)
            FROM lego_features
            WHERE date BETWEEN ? AND ?
              AND (active_gate_source_date IS NULL
                   OR date_diff('day', active_gate_source_date, date) > {max_gap_days})""",
        [start, end],
    ).fetchone()
    exit_signal_dates = [
        value.date().isoformat()
        for value in gate.loc[
            (gate["active_gate_event"] == "结束")
            & (gate["gate_date"] >= pd.Timestamp(start))
            & (gate["gate_date"] <= pd.Timestamp(end)),
            "gate_date",
        ]
    ]
    stale_exit = connection.execute(
        f"""SELECT min(date) FROM lego_features
            WHERE date BETWEEN ? AND ?
              AND coalesce(active_gate_source_enabled, FALSE)
              AND (active_gate_source_date IS NULL
                   OR date_diff('day', active_gate_source_date, date) > {max_gap_days})""",
        [start, end],
    ).fetchone()[0]
    if stale_exit is not None:
        exit_signal_dates.append(pd.Timestamp(stale_exit).date().isoformat())
    exit_signal_dates = sorted(set(exit_signal_dates))
    try:
        source_label = str(source.relative_to(ROOT))
    except ValueError:
        source_label = str(source)
    return {
        "provider": provider,
        "source_file": source_label,
        "quality": payload.get("quality", "ohlcv" if provider == "compass" else None),
        "stale": bool(payload.get("stale", False)),
        "as_of": payload.get("as_of") or payload.get("database_as_of"),
        "validation": payload.get("validation") if provider == "ths_internal_proxy" else None,
        "rows": int(len(gate)),
        "start": str(gate["gate_date"].min().date()) if not gate.empty else None,
        "end": str(gate["gate_date"].max().date()) if not gate.empty else None,
        "enabled_days": int(gate["active_market_gate"].sum()) if not gate.empty else 0,
        "exit_reasons": gate.loc[gate["active_gate_event"] == "结束", "active_gate_reason"].value_counts().to_dict() if not gate.empty else {},
        "exit_signal_dates": exit_signal_dates,
        "thresholds": {
            "start_one": float(block.params.get("start_one", 4.0)),
            "use_two_day": bool(block.params.get("use_two_day", False)),
            "start_two": float(block.params.get("start_two", 5.0)),
            "exit_one": float(block.params.get("exit_one", -2.33)),
            "ma_period": int(block.params.get("ma_period", 10)),
            "ma_buffer": float(block.params.get("ma_buffer", 0.0)),
        },
        "max_asof_gap_days": int(gap[1] or 0),
        "gap_closed_rows": int(gap_closed[0] or 0),
        "gap_closed_dates": int(gap_closed[1] or 0),
        "gap_policy": "超过最大缺口后强制关闭门控，不沿用旧值、不使用代理数据",
    }


def _factor_research(
    connection: duckdb.DuckDBPyConnection,
    descriptors: list[dict[str, Any]],
    start: date,
    end: date,
    cache_key: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Build compact PandaFactor-style diagnostics from point-in-time rows."""
    factors = [item for item in descriptors if item["block"].category == "factor"]
    summary_columns = [
        "block_id", "name", "weight", "direction", "method", "observations", "dates",
        "ic_5", "rank_ic_5", "ic_20", "rank_ic_20", "icir_20",
        "positive_rate_20", "spread_5", "spread_20",
    ]
    group_columns = [
        "block_id", "name", "bucket", "label", "forward_return_5",
        "forward_return_20", "observations", "dates",
    ]
    correlation_columns = [
        "block_a", "name_a", "block_b", "name_b", "correlation", "dates",
    ]
    if not factors:
        return (
            pd.DataFrame(columns=summary_columns),
            pd.DataFrame(columns=group_columns),
            pd.DataFrame(columns=correlation_columns),
        )

    research_cache = PRODUCT_WORK_DIR / "cache" / "research" / cache_key if cache_key else None
    if research_cache and (research_cache / "factor-analysis.parquet").is_file():
        try:
            analysis = pd.read_parquet(research_cache / "factor-analysis.parquet")
            groups = pd.read_parquet(research_cache / "factor-groups.parquet")
            correlations = pd.read_parquet(research_cache / "factor-correlations.parquet")
            weights = {item["block"].id: float(item["weight"]) for item in factors}
            if not analysis.empty:
                analysis["weight"] = analysis["block_id"].map(weights).fillna(analysis["weight"])
            return analysis, groups, correlations
        except Exception:
            pass

    horizon_end = pd.Timestamp(end) + pd.Timedelta(days=120)
    connection.execute(
        """
        CREATE TEMP TABLE lego_forward_returns AS
        WITH bars AS (
            SELECT d.instrument_id, d.date, d.close,
                   lead(d.close, 5) OVER (
                       PARTITION BY d.instrument_id ORDER BY d.date
                   ) AS close_5,
                   lead(d.close, 20) OVER (
                       PARTITION BY d.instrument_id ORDER BY d.date
                   ) AS close_20
            FROM daily_bar d
            WHERE d.date BETWEEN ? AND ?
              AND d.instrument_id IN (
                  SELECT DISTINCT instrument_id FROM lego_ranked
              )
              AND d.close > 0
        )
        SELECT instrument_id, date,
               close_5 / nullif(close, 0) - 1 AS forward_return_5,
               close_20 / nullif(close, 0) - 1 AS forward_return_20
        FROM bars
        """,
        [start, horizon_end.date()],
    )

    def finite_mean(values: pd.Series) -> float | None:
        clean = pd.to_numeric(values, errors="coerce")
        clean = clean[clean.map(math.isfinite)]
        return float(clean.mean()) if not clean.empty else None

    summary_rows: list[dict[str, Any]] = []
    group_rows: list[dict[str, Any]] = []
    for factor_index, item in enumerate(factors):
        block = item["block"]
        direction = str(block.params.get("direction", "higher"))
        sign = -1.0 if direction == "lower" else 1.0
        raw_alias = item["raw"]
        research_table = f"lego_factor_research_{factor_index}"
        connection.execute(
            f"""
            CREATE TEMP TABLE {research_table} AS
            WITH valid AS (
                SELECT r.date, ({sign} * r.{raw_alias})::DOUBLE AS factor_value,
                       f.forward_return_5, f.forward_return_20
                FROM lego_ranked r
                JOIN lego_forward_returns f USING (instrument_id, date)
                WHERE r.{raw_alias} IS NOT NULL
                  AND isfinite(r.{raw_alias})
                  AND (f.forward_return_5 IS NOT NULL OR f.forward_return_20 IS NOT NULL)
            )
            SELECT *,
                   percent_rank() OVER (
                       PARTITION BY date ORDER BY factor_value
                   ) AS factor_rank,
                   percent_rank() OVER (
                       PARTITION BY date ORDER BY forward_return_5
                   ) AS return_rank_5,
                   percent_rank() OVER (
                       PARTITION BY date ORDER BY forward_return_20
                   ) AS return_rank_20,
                   ntile(5) OVER (
                       PARTITION BY date ORDER BY factor_value
                   ) AS bucket
            FROM valid
            """
        )
        daily_ic = connection.execute(
            f"""
            SELECT date, count(*) AS observations,
                   corr(factor_value, forward_return_5) AS ic_5,
                   corr(factor_rank, return_rank_5) AS rank_ic_5,
                   corr(factor_value, forward_return_20) AS ic_20,
                   corr(factor_rank, return_rank_20) AS rank_ic_20
            FROM {research_table}
            GROUP BY date
            HAVING count(*) >= 5
            ORDER BY date
            """
        ).df()
        grouped = connection.execute(
            f"""
            WITH daily_groups AS (
                SELECT date, bucket,
                       avg(forward_return_5) AS forward_return_5,
                       avg(forward_return_20) AS forward_return_20,
                       count(*) AS observations
                FROM {research_table}
                GROUP BY date, bucket
            )
            SELECT bucket,
                   avg(forward_return_5) AS forward_return_5,
                   avg(forward_return_20) AS forward_return_20,
                   sum(observations) AS observations,
                   count(*) AS dates
            FROM daily_groups
            GROUP BY bucket
            ORDER BY bucket
            """
        ).df()
        connection.execute(f"DROP TABLE {research_table}")
        group_map = {int(row["bucket"]): row for _, row in grouped.iterrows()}
        low = group_map.get(1)
        high = group_map.get(5)
        rank_ic_20 = pd.to_numeric(daily_ic.get("rank_ic_20"), errors="coerce")
        rank_ic_20 = rank_ic_20[rank_ic_20.map(math.isfinite)]
        rank_ic_std = float(rank_ic_20.std(ddof=1)) if len(rank_ic_20) > 1 else 0.0
        summary_rows.append({
            "block_id": block.id,
            "name": block.name,
            "weight": float(item["weight"]),
            "direction": direction,
            "method": str(block.params.get("method", "robust_sigmoid")),
            "observations": int(daily_ic["observations"].sum()) if not daily_ic.empty else 0,
            "dates": int(len(daily_ic)),
            "ic_5": finite_mean(daily_ic.get("ic_5", pd.Series(dtype=float))),
            "rank_ic_5": finite_mean(daily_ic.get("rank_ic_5", pd.Series(dtype=float))),
            "ic_20": finite_mean(daily_ic.get("ic_20", pd.Series(dtype=float))),
            "rank_ic_20": finite_mean(rank_ic_20),
            "icir_20": float(rank_ic_20.mean() / rank_ic_std) if rank_ic_std > 0 else None,
            "positive_rate_20": float((rank_ic_20 > 0).mean()) if not rank_ic_20.empty else None,
            "spread_5": (
                float(high["forward_return_5"] - low["forward_return_5"])
                if high is not None and low is not None
                and pd.notna(high["forward_return_5"]) and pd.notna(low["forward_return_5"])
                else None
            ),
            "spread_20": (
                float(high["forward_return_20"] - low["forward_return_20"])
                if high is not None and low is not None
                and pd.notna(high["forward_return_20"]) and pd.notna(low["forward_return_20"])
                else None
            ),
        })
        for _, row in grouped.iterrows():
            bucket = int(row["bucket"])
            group_rows.append({
                "block_id": block.id,
                "name": block.name,
                "bucket": bucket,
                "label": f"第{bucket}组" + (" · 因子偏好" if bucket == 5 else ""),
                "forward_return_5": float(row["forward_return_5"]) if pd.notna(row["forward_return_5"]) else None,
                "forward_return_20": float(row["forward_return_20"]) if pd.notna(row["forward_return_20"]) else None,
                "observations": int(row["observations"]),
                "dates": int(row["dates"]),
            })

    correlation_rows: list[dict[str, Any]] = []
    for left_index, left in enumerate(factors):
        for right in factors[left_index + 1:]:
            correlation = connection.execute(
                f"""
                WITH daily AS (
                    SELECT date, corr({left['score']}, {right['score']}) AS value,
                           count(*) AS observations
                    FROM lego_ranked
                    WHERE {left['score']} IS NOT NULL AND {right['score']} IS NOT NULL
                    GROUP BY date
                    HAVING count(*) >= 5
                )
                SELECT avg(value), count(*) FROM daily WHERE isfinite(value)
                """
            ).fetchone()
            correlation_rows.append({
                "block_a": left["block"].id,
                "name_a": left["block"].name,
                "block_b": right["block"].id,
                "name_b": right["block"].name,
                "correlation": float(correlation[0]) if correlation and correlation[0] is not None else None,
                "dates": int(correlation[1] or 0) if correlation else 0,
            })

    result = (
        pd.DataFrame(summary_rows, columns=summary_columns),
        pd.DataFrame(group_rows, columns=group_columns),
        pd.DataFrame(correlation_rows, columns=correlation_columns),
    )
    if research_cache:
        temporary = research_cache.with_name(f".{research_cache.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.mkdir(parents=True, exist_ok=False)
            result[0].to_parquet(temporary / "factor-analysis.parquet", index=False)
            result[1].to_parquet(temporary / "factor-groups.parquet", index=False)
            result[2].to_parquet(temporary / "factor-correlations.parquet", index=False)
            research_cache.parent.mkdir(parents=True, exist_ok=True)
            if not research_cache.exists():
                temporary.replace(research_cache)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
    return result


def _is_duckdb_lock_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "could not set lock" in message or "conflicting lock" in message


def _cached_security_name(code: str) -> str:
    if not LEGACY_EOD_CACHE_DB.exists():
        return code
    try:
        with duckdb.connect(str(LEGACY_EOD_CACHE_DB), read_only=True) as connection:
            row = connection.execute(
                "SELECT name FROM security_master WHERE code=? LIMIT 1", [code]
            ).fetchone()
        return str(row[0] or code) if row else code
    except Exception:
        return code


def _install_factor_example_catalog(
    connection: duckdb.DuckDBPyConnection,
    instrument_id: str,
    code: str,
    exchange: str,
) -> str:
    name = _cached_security_name(code)
    connection.execute(
        """CREATE TEMP TABLE instrument_master(
               instrument_id VARCHAR, code VARCHAR, name VARCHAR,
               asset_type VARCHAR, exchange VARCHAR
           )"""
    )
    connection.execute(
        "INSERT INTO instrument_master VALUES (?, ?, ?, 'stock', ?)",
        [instrument_id, code, name, exchange],
    )
    return name


@contextmanager
def _factor_example_connection(
    instrument_id: str,
    code: str,
    exchange: str,
):
    """Open the live market DB, falling back to local read-only K-line caches.

    DuckDB intentionally blocks cross-process readers while an updater owns a
    write lock.  Factor teaching must remain available during the daily update,
    so it can use the existing adjusted/EOD caches without interrupting that
    writer.  Full backtests continue to require the authoritative market DB.
    """
    primary: duckdb.DuckDBPyConnection | None = None
    try:
        primary = duckdb.connect(str(MARKET_DB), read_only=True)
    except Exception as exc:
        if not _is_duckdb_lock_error(exc):
            raise
    if primary is not None:
        try:
            yield primary, "local-duckdb"
        finally:
            primary.close()
        return

    if ADJUSTED_KLINE_CACHE_DB.exists():
        adjusted = duckdb.connect(str(ADJUSTED_KLINE_CACHE_DB), read_only=True)
        try:
            available = adjusted.execute(
                "SELECT count(*) FROM adjusted_bar WHERE instrument_id=?",
                [instrument_id],
            ).fetchone()[0]
            if available:
                adjusted.execute(
                    """CREATE TEMP VIEW daily_bar AS
                       SELECT date, instrument_id, open, high, low, close, volume, amount
                       FROM adjusted_bar
                       QUALIFY row_number() OVER (
                           PARTITION BY instrument_id, date
                           ORDER BY (adjustment='前复权') DESC, updated_at DESC
                       )=1"""
                )
                _install_factor_example_catalog(adjusted, instrument_id, code, exchange)
                yield adjusted, "adjusted-kline-cache"
                return
        finally:
            adjusted.close()

    if LEGACY_EOD_CACHE_DB.exists():
        fallback = duckdb.connect(":memory:")
        try:
            escaped_path = str(LEGACY_EOD_CACHE_DB).replace("'", "''")
            fallback.execute(f"ATTACH '{escaped_path}' AS legacy (READ_ONLY)")
            available = fallback.execute(
                "SELECT count(*) FROM legacy.daily_bar WHERE code=?", [code]
            ).fetchone()[0]
            if available:
                fallback.execute(
                    """CREATE TEMP VIEW daily_bar AS
                       SELECT date,
                              CASE WHEN code LIKE '4%' OR code LIKE '8%' OR code LIKE '92%' THEN 'BJ.'||code
                                   WHEN code LIKE '5%' OR code LIKE '6%' OR code LIKE '9%' THEN 'SH.'||code
                                   ELSE 'SZ.'||code END AS instrument_id,
                              open, high, low, close, volume, amount
                       FROM legacy.daily_bar"""
                )
                _install_factor_example_catalog(fallback, instrument_id, code, exchange)
                yield fallback, "eod-cache"
                return
        finally:
            fallback.close()

    raise RuntimeError(
        "本地行情库正在更新，且该股票没有可用的只读 K 线缓存；请稍后刷新案例"
    )


def factor_teaching_example(
    factor_type: str,
    code: str = "600519",
    params: dict[str, Any] | None = None,
    limit: int = 120,
) -> dict[str, Any]:
    """Calculate one factor on a real local security for an educational chart."""
    if factor_type not in FACTOR_REGISTRY and factor_type != "factor_custom_formula":
        raise ValueError("只支持节点库中的因子案例")
    if not re.fullmatch(r"\d{6}", str(code)):
        raise ValueError("案例股票代码必须是 6 位数字")
    limit = max(60, min(240, int(limit)))
    exchange = "BJ" if code.startswith(("4", "8", "92")) else "SH" if code.startswith(("5", "6", "9")) else "SZ"
    instrument_id = f"{exchange}.{code}"
    defaults = dict(BLOCK_CATALOG[factor_type]["params"])
    defaults.update(dict(params or {}))
    block = BlockSpec(
        id="teaching-factor", type=factor_type, category="factor",
        name=BLOCK_CATALOG[factor_type]["name"], params=defaults,
    )
    spec = PipelineSpec(
        id="teaching-example", name="因子教学案例", description="", version=1,
        scope="custom", start="2000-01-01", end="2099-12-31",
        initial_cash=1_000_000.0, blocks=[block],
    )
    requirements = _requirements(spec)
    expression = _raw_factor(block)
    if not MARKET_DB.exists():
        raise FileNotFoundError(f"本地行情库不存在: {MARKET_DB}")
    with _factor_example_connection(instrument_id, code, exchange) as (connection, data_source):
        security = connection.execute(
            """SELECT m.name, max(d.date) AS last_date
               FROM instrument_master m JOIN daily_bar d USING (instrument_id)
               WHERE m.instrument_id=? GROUP BY m.name""",
            [instrument_id],
        ).fetchone()
        if not security or security[1] is None:
            raise ValueError(f"本地行情库没有 {code} 的日线数据")
        end = pd.Timestamp(security[1]).normalize()
        warmup_days = max(900, int(requirements["longest"]) * 6)
        start = end - pd.Timedelta(days=warmup_days)
        scope_clause = f"m.instrument_id='{instrument_id}'"
        connection.execute(
            "CREATE TEMP TABLE lego_features AS " + _feature_sql(scope_clause, requirements),
            [start.date(), end.date()],
        )
        if requirements.get("recursive_indicators"):
            _attach_recursive_indicators(connection, requirements)
        frame = connection.execute(
            f"""SELECT date, open, high, low, close, volume, amount,
                       ({expression})::DOUBLE AS factor_value
                FROM lego_features ORDER BY date"""
        ).df()
    if frame.empty:
        raise ValueError(f"{code} 在案例区间内没有可用行情")
    frame["factor_value"] = pd.to_numeric(frame["factor_value"], errors="coerce")
    frame.loc[~np.isfinite(frame["factor_value"]), "factor_value"] = np.nan
    frame["forward_5"] = pd.to_numeric(frame["close"], errors="coerce").shift(-5) / pd.to_numeric(frame["close"], errors="coerce") - 1
    valid = frame["factor_value"].dropna()
    direction = str(defaults.get("direction", "higher"))
    if valid.empty:
        preferred = pd.Series(False, index=frame.index)
    elif factor_type in _BINARY_FACTOR_TYPES:
        preferred = frame["factor_value"] >= 0.5
    elif direction == "lower":
        preferred = frame["factor_value"] <= float(valid.quantile(0.20))
    else:
        preferred = frame["factor_value"] >= float(valid.quantile(0.80))
    candidates = frame[preferred & frame["forward_5"].notna()]
    if candidates.empty:
        candidates = frame[frame["factor_value"].notna() & frame["forward_5"].notna()]
    event = candidates.iloc[-1] if not candidates.empty else None
    if event is not None:
        event_positions = frame.index[frame["date"] == event["date"]].tolist()
        event_position = int(event_positions[-1]) if event_positions else len(frame) - 1
        display_start = max(0, event_position - int(limit * 0.68))
        display_end = min(len(frame), display_start + limit)
        display_start = max(0, display_end - limit)
        display = frame.iloc[display_start:display_end].copy()
    else:
        display = frame.tail(limit).copy()
    values = display["factor_value"].dropna()
    percentile = display["factor_value"].rank(pct=True) * 100.0
    if direction == "lower":
        percentile = 100.0 - percentile
    display["factor_score"] = percentile
    candles = []
    for row in display.itertuples(index=False):
        value = float(row.factor_value) if pd.notna(row.factor_value) else None
        score = float(row.factor_score) if pd.notna(row.factor_score) else None
        candles.append({
            "date": pd.Timestamp(row.date).date().isoformat(),
            "open": float(row.open), "high": float(row.high), "low": float(row.low),
            "close": float(row.close), "volume": float(row.volume or 0),
            "amount": float(row.amount or 0), "factor_value": value,
            "factor_score": score,
        })
    metadata = BLOCK_CATALOG[factor_type]
    if factor_type == "factor_custom_formula":
        metadata = {
            **metadata,
            "family": "自定义公式",
            "formula": str(defaults.get("formula") or ""),
            "plain_explanation": "按照你输入的安全公式逐日计算；只允许白名单行情字段和滚动函数。",
            "how_to_read": "方向设为 higher 时数值越大得分越高；设为 lower 时数值越小得分越高。",
            "best_for": "把自己的技术判断转成可重复计算、可回测的连续分值。",
            "pitfall": "公式能运行不代表有效，仍需检查覆盖率、极值、Rank IC、分组收益和样本外稳定性。",
            "example_caption": "修改公式后点击刷新案例，对照 K 线检查数值变化是否符合原始交易想法。",
        }
    event_payload = None if event is None else {
        "date": pd.Timestamp(event["date"]).date().isoformat(),
        "factor_value": float(event["factor_value"]),
        "close": float(event["close"]),
        "forward_5_return": float(event["forward_5"]),
        "note": "这是历史案例复盘；后续5日收益只用于解释，不进入因子计算或回测信号。",
    }
    return {
        "factor_type": factor_type, "factor_name": metadata["name"],
        "code": code, "name": str(security[0] or code), "source": data_source,
        "as_of": end.date().isoformat(),
        "chart_from": pd.Timestamp(display["date"].iloc[0]).date().isoformat(),
        "chart_to": pd.Timestamp(display["date"].iloc[-1]).date().isoformat(),
        "params": defaults,
        "direction": direction, "candles": candles, "example": event_payload,
        "statistics": {
            "count": int(values.count()),
            "min": float(values.min()) if not values.empty else None,
            "median": float(values.median()) if not values.empty else None,
            "max": float(values.max()) if not values.empty else None,
            "latest": float(values.iloc[-1]) if not values.empty else None,
        },
        "teaching": {
            key: metadata.get(key) for key in (
                "plain_explanation", "how_to_read", "best_for", "pitfall",
                "example_caption", "formula", "family",
            )
        },
    }


def run_lego_backtest(spec: PipelineSpec | dict[str, Any], persist: bool = True) -> dict:
    total_started = time.perf_counter()
    pipeline = validate_pipeline(spec)
    if not MARKET_DB.exists():
        raise FileNotFoundError(f"本地行情库不存在: {MARKET_DB}")
    universe = _one(pipeline, "universe")
    scope, scope_clause, asset_type = _scope_clause(universe)
    start = pd.Timestamp(pipeline.start).normalize()
    end = pd.Timestamp(pipeline.end).normalize()
    req = _requirements(pipeline)
    warmup_start = start - pd.Timedelta(days=max(400, int(req["longest"]) * 5))
    groups = _filter_groups(pipeline)
    cumulative = "TRUE"
    funnel_rows: list[dict[str, Any]] = []
    rank_block = _one(pipeline, "rank")
    portfolio_block = _one(pipeline, "portfolio")
    top_k = int(rank_block.params.get("top_k", 50))
    positions = int(portfolio_block.params.get("positions", 10))
    rebalance = int(portfolio_block.params.get("rebalance_every", 5))
    active_gate_info: dict[str, Any] = {}
    factor_analysis = pd.DataFrame()
    factor_groups = pd.DataFrame()
    factor_correlations = pd.DataFrame()
    feature_source = "dynamic"
    feature_fallback_reason: str | None = None
    feature_seconds = 0.0
    funnel_seconds = 0.0
    score_seconds = 0.0
    research_seconds = 0.0
    replay_seconds = 0.0

    from .precompute import market_snapshot, precompute_status
    from .result_cache import (
        create_cached_run,
        enabled as result_cache_enabled,
        load_cached_result,
        make_cache_key,
        store_cached_result,
    )

    dataset = market_snapshot()
    feature_state = precompute_status()
    cache_key: str | None = None
    if persist and result_cache_enabled() and int(dataset.get("dataset_version") or 0) > 0:
        cache_key = make_cache_key(
            asdict(pipeline),
            int(dataset["dataset_version"]),
            int(feature_state.get("schema_version") or 0),
            str(feature_state.get("calculator_version") or "dynamic"),
        )
        cached = load_cached_result(cache_key)
        if cached is not None:
            source_run_id = str(cached.get("run_id") or "") or None
            cached["timings"] = {
                **dict(cached.get("timings") or {}),
                "total_seconds": round(time.perf_counter() - total_started, 6),
            }
            cached["cache_info"] = {
                **dict(cached.get("cache_info") or {}),
                "dataset_version": int(dataset["dataset_version"]),
                "feature_source": "result-cache",
                "fallback_reason": None,
            }
            return create_cached_run(cached, cache_key, True, source_run_id)

    fees = {
        "commission": 0.0003, "min_commission": 5.0,
        "transfer_fee": 0.00001, "stamp_tax": 0.0005,
        "slippage": 0.0005, "invest_ratio": 0.95, "max_position_pct": 0.10,
        "max_participation": 0.10, "lot_size": 100,
        **pipeline.fees, **_one(pipeline, "execution").params,
    }
    config = BacktestConfig(
        strategy="momentum", codes=[], start=start.date(), end=end.date(),
        universe_scope="all" if scope in {"all", "bj", "custom"} else "sh_sz",
        initial_cash=pipeline.initial_cash, execution_mode="next_open",
        commission=float(fees["commission"]), min_commission=float(fees["min_commission"]),
        transfer_fee=float(fees["transfer_fee"]), stamp_tax=float(fees["stamp_tax"]),
        slippage=float(fees["slippage"]), invest_ratio=float(fees["invest_ratio"]),
        lot_size=int(fees["lot_size"]),
        max_participation=float(fees["max_participation"]),
        params={"lookback": 20, "top_n": positions, "rebalance": rebalance},
    )
    replay_pool: ThreadPoolExecutor | None = None
    replay_future = None
    replay_started = 0.0

    with duckdb.connect(str(MARKET_DB), read_only=True) as connection:
        duckdb_threads = max(2, min(8, int(os.environ.get("QUANTWEAVE_DUCKDB_THREADS", "8"))))
        connection.execute(f"SET threads={duckdb_threads}")
        calendar_scope_clause = (
            "m.exchange IN ('SH', 'SZ')" if asset_type == "fund" else
            "m.exchange IN ('SH', 'SZ', 'BJ')" if asset_type == "mixed" else
            scope_clause
        )
        feature_started = time.perf_counter()
        from .precompute import attach_precomputed_features

        feature_expressions = [group["expression"] for group in groups]
        feature_expressions.extend(
            _raw_factor(block)
            for block in (_enabled(pipeline, "factor") + _enabled(pipeline, "penalty"))
        )
        feature_columns = {
            "date", "instrument_id", "code", "name", "exchange",
            "open", "high", "low", "close", "volume", "amount",
            "first_date", "prev_close", "prev_volume", "daily_return",
            "listing_days", "adv_20",
        }
        for expression in feature_expressions:
            feature_columns.update(
                token
                for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", expression)
                if re.fullmatch(
                    r"(?:lag_close|ma|adv|vol|peak|max_close|min_close|max_high|min_low|"
                    r"rsi|atr|vma|prev_vma|path_length|std_close|std_amount|downside_vol|"
                    r"avg_range|mfi|cmf|ad_strength|pv_corr|amihud|cci)_\d+(?:_lag_\d+)?",
                    token,
                )
            )

        precomputed, feature_fallback_reason = attach_precomputed_features(
            connection,
            req,
            start.date(),
            end.date(),
            scope,
            asset_type,
            sorted(feature_columns),
        )
        if precomputed:
            feature_source = "precomputed"
        else:
            connection.execute(
                "CREATE TEMP TABLE lego_features AS " + _feature_sql(
                    scope_clause, req, calendar_scope_clause, asset_type
                ),
                [warmup_start.date(), end.date()],
            )
        feature_seconds = time.perf_counter() - feature_started
        if req.get("recursive_indicators"):
            _attach_recursive_indicators(connection, req)
        active_gate_blocks = [
            block for block in _enabled(pipeline)
            if block.type in {"filter_active_market_gate", "filter_ths_active_market_proxy_gate"}
        ]
        if len(active_gate_blocks) > 1:
            raise ValueError("一个工作流只能启用一个活跃市值门控")
        if active_gate_blocks:
            active_gate_info = _attach_active_market_gate(
                connection, active_gate_blocks[0], start.date(), end.date()
            )
        funnel_started = time.perf_counter()
        raw_before = connection.execute(
            f"""SELECT count(*) FROM daily_bar d JOIN instrument_master m USING(instrument_id)
                WHERE {_asset_clause(asset_type)} AND regexp_matches(coalesce(m.code,''),'^[0-9]{{6}}$')
                  AND {scope_clause} AND d.date BETWEEN ? AND ?""",
            [start.date(), end.date()],
        ).fetchone()[0]
        funnel_predicates = ["TRUE"]
        for group in groups:
            funnel_predicates.append(
                f"({funnel_predicates[-1]}) AND ({group['expression']})"
            )
        funnel_counts = _count_many(
            connection, funnel_predicates, start.date(), end.date()
        )
        universe_after = funnel_counts[0]
        funnel_rows.append({
            "stage": 1, "category": "universe", "block_id": universe.id,
            "name": universe.name, "before": int(raw_before or 0),
            "after": universe_after["rows"],
            "removed": max(0, int(raw_before or 0) - universe_after["rows"]),
            "before_instruments": None, "after_instruments": universe_after["instruments"],
            "dates": universe_after["dates"],
            "reason": f"数据源={scope}；资产={asset_type}；剔除无效价格与低于此前20日覆盖中位数80%的稀疏日期",
            "combine": "AND", "block_ids": [universe.id],
        })
        stage_no = 2
        for group_index, group in enumerate(groups):
            before = funnel_counts[group_index]
            next_predicate = funnel_predicates[group_index + 1]
            after = funnel_counts[group_index + 1]
            blocks = group["blocks"]
            funnel_rows.append({
                "stage": stage_no, "category": group["category"],
                "block_id": blocks[0].id, "block_ids": [block.id for block in blocks],
                "name": group["name"], "before": before["rows"], "after": after["rows"],
                "removed": max(0, before["rows"] - after["rows"]),
                "before_instruments": before["instruments"],
                "after_instruments": after["instruments"], "dates": after["dates"],
                "reason": "同组按 " + "/".join(block.combine for block in blocks[1:]) + " 组合；未使用未来数据"
                if len(blocks) > 1 else BLOCK_CATALOG[blocks[0].type]["description"],
                "combine": "/".join(block.combine for block in blocks[1:]) or "AND",
            })
            cumulative = next_predicate
            stage_no += 1
        eligible = funnel_counts[-1]
        funnel_seconds = time.perf_counter() - funnel_started
        score_started = time.perf_counter()
        score_sql, descriptors = _score_sql(pipeline, cumulative)
        connection.execute(
            "CREATE TEMP TABLE lego_ranked AS " + score_sql,
            [start.date(), end.date()],
        )
        factor_blocks = _enabled(pipeline, "factor")
        penalty_blocks = _enabled(pipeline, "penalty")
        for block in factor_blocks:
            funnel_rows.append({
                "stage": stage_no, "category": block.category, "block_id": block.id,
                "block_ids": [block.id], "name": block.name,
                "before": eligible["rows"], "after": eligible["rows"], "removed": 0,
                "before_instruments": eligible["instruments"],
                "after_instruments": eligible["instruments"], "dates": eligible["dates"],
                "reason": f"评分积木：weight={float(block.params.get('weight', 0)):.4g}，"
                          f"method={block.params.get('method', 'percentile')}；不在此步硬删除股票",
                "combine": "WEIGHTED_ADD",
            })
            stage_no += 1
        normalizer = _one(pipeline, "normalize")
        funnel_rows.append({
            "stage": stage_no, "category": "normalize", "block_id": normalizer.id,
            "block_ids": [normalizer.id], "name": normalizer.name,
            "before": eligible["rows"], "after": eligible["rows"], "removed": 0,
            "before_instruments": eligible["instruments"],
            "after_instruments": eligible["instruments"], "dates": eligible["dates"],
            "reason": f"median/MAD 稳健标准化；S形 k={float(normalizer.params.get('sigmoid_k', 1.0)):.4g}，"
                      f"z 截断 ±{float(normalizer.params.get('clip', 6.0)):.4g}；百分位因子保持横截面百分位",
            "combine": "NORMALIZE",
        })
        stage_no += 1
        for block in penalty_blocks:
            funnel_rows.append({
                "stage": stage_no, "category": block.category, "block_id": block.id,
                "block_ids": [block.id], "name": block.name,
                "before": eligible["rows"], "after": eligible["rows"], "removed": 0,
                "before_instruments": eligible["instruments"],
                "after_instruments": eligible["instruments"], "dates": eligible["dates"],
                "reason": f"风险扣分积木：weight={float(block.params.get('weight', 0)):.4g}，"
                          f"method={block.params.get('method', 'percentile')}；不在此步硬删除股票",
                "combine": "WEIGHTED_SUBTRACT",
            })
            stage_no += 1
        rank_count = connection.execute(
            "SELECT count(*), count(DISTINCT instrument_id), count(DISTINCT date) FROM lego_ranked WHERE candidate_rank<=?",
            [top_k],
        ).fetchone()
        funnel_rows.append({
            "stage": stage_no, "category": "rank", "block_id": rank_block.id,
            "block_ids": [rank_block.id], "name": rank_block.name,
            "before": eligible["rows"], "after": int(rank_count[0] or 0),
            "removed": max(0, eligible["rows"] - int(rank_count[0] or 0)),
            "before_instruments": eligible["instruments"],
            "after_instruments": int(rank_count[1] or 0), "dates": int(rank_count[2] or 0),
            "reason": f"每个交易日按最终分从高到低保留 Top {top_k}", "combine": "RANK",
        })
        stage_no += 1
        detail_columns = [
            "date AS signal_date", "instrument_id", "code", "name", "exchange",
            "close AS signal_close", "final_score", "candidate_rank", "adv_20 AS liquidity20",
        ]
        if active_gate_info:
            detail_columns.extend([
                "active_market_gate", "active_one_day", "active_two_day",
                "active_close", "active_ma", "active_ma_threshold",
                "active_gate_event", "active_gate_reason",
            ])
        if req.get("zettaranc"):
            detail_columns.extend([
                "z_signal", "z_resonance_score", "z_line_state",
                "z_brick_color", "z_exit_reason",
                "z_b1_standard", "z_b2_standard", "z_b3_standard",
                "z_exit_risk_standard",
            ])
        for descriptor in descriptors:
            # Block ids may start with a digit (UUID-style) or contain a dash.
            # They are validated above, then quoted here as SQL identifiers so
            # UI-generated ids remain valid without lossy renaming/collisions.
            block_id = descriptor["block"].id
            detail_columns.extend([
                f'{descriptor["raw"]} AS "{block_id}__raw"',
                f'{descriptor["score"]} AS "{block_id}__score"',
                f'{descriptor["contribution"]} AS "{block_id}__contribution"',
            ])
        rank_details = connection.execute(
            f"SELECT {', '.join(detail_columns)} FROM lego_ranked "
            "WHERE candidate_rank<=? ORDER BY signal_date,candidate_rank",
            [top_k],
        ).df()
        score_seconds = time.perf_counter() - score_started
        candidates = rank_details[rank_details["candidate_rank"] <= positions].copy()
        candidates = candidates.rename(columns={"final_score": "score"})
        candidate_columns = [
            "signal_date", "instrument_id", "code", "name", "exchange",
            "signal_close", "score", "candidate_rank", "liquidity20",
        ]
        candidate_columns.extend(
            column for column in (
                "z_signal", "z_resonance_score", "z_line_state",
                "z_brick_color", "z_exit_reason",
            ) if column in candidates.columns
        )
        candidates = candidates[candidate_columns]
        if candidates.empty:
            raise ValueError("流水线没有产生任何候选；请检查筛选阈值或所选日期")
        replay_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quantweave-replay")
        replay_started = time.perf_counter()
        replay_future = replay_pool.submit(
            run_full_market_backtest,
            config,
            scope="all" if scope in {"all", "bj", "custom"} else "sh_sz",
            top_n=positions,
            rebalance=rebalance,
            persist=False,
            _candidates=candidates,
            _scope_clause_override=None,
            _active_gate_exit_signal_dates=(
                active_gate_info.get("exit_signal_dates") if active_gate_info else None
            ),
            _max_position_pct=float(fees["max_position_pct"]),
        )
        research_signature = {
            "version": "factor-research-v1",
            "dataset_version": int(dataset.get("dataset_version") or 0),
            "start": start.date().isoformat(),
            "end": end.date().isoformat(),
            "scope": scope,
            "filters": [group["expression"] for group in groups],
            "normalizer": _one(pipeline, "normalize").params,
            "factors": [
                {
                    "id": item["block"].id,
                    "type": item["block"].type,
                    "name": item["block"].name,
                    "params": {
                        key: value
                        for key, value in item["block"].params.items()
                        if key != "weight"
                    },
                }
                for item in descriptors
                if item["block"].category == "factor"
            ],
        }
        research_key = hashlib.sha256(
            json.dumps(
                research_signature,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        research_started = time.perf_counter()
        try:
            factor_analysis, factor_groups, factor_correlations = _factor_research(
                connection, descriptors, start.date(), end.date(), research_key
            )
        except Exception:
            replay_pool.shutdown(wait=False, cancel_futures=True)
            raise
        research_seconds = time.perf_counter() - research_started
        portfolio_count = len(candidates)
        funnel_rows.append({
            "stage": stage_no, "category": "portfolio", "block_id": portfolio_block.id,
            "block_ids": [portfolio_block.id], "name": portfolio_block.name,
            "before": int(rank_count[0] or 0), "after": int(portfolio_count),
            "removed": max(0, int(rank_count[0] or 0) - int(portfolio_count)),
            "before_instruments": int(rank_count[1] or 0),
            "after_instruments": int(candidates["instrument_id"].nunique()) if not candidates.empty else 0,
            "dates": int(candidates["signal_date"].nunique()) if not candidates.empty else 0,
            "reason": f"每日取前 {positions} 名，等权，间隔 {rebalance} 个交易日调仓",
            "combine": "PORTFOLIO",
        })

    if replay_future is None or replay_pool is None:
        if replay_pool is not None:
            replay_pool.shutdown(wait=False, cancel_futures=True)
        raise RuntimeError("回测撮合任务未启动")
    result = replay_future.result()
    replay_seconds = time.perf_counter() - replay_started
    replay_pool.shutdown(wait=True)
    execution = _one(pipeline, "execution")
    executable_candidates = int(result.get("coverage", {}).get("candidate_rows", 0))
    buy_trades = result["trades"][result["trades"]["side"] == "买入"] if not result["trades"].empty else result["trades"]
    funnel_rows.append({
        "stage": len(funnel_rows) + 1, "category": "execution", "block_id": execution.id,
        "block_ids": [execution.id], "name": execution.name,
        "before": executable_candidates, "after": int(len(buy_trades)),
        "removed": max(0, executable_candidates - int(len(buy_trades))),
        "before_instruments": int(candidates["instrument_id"].nunique()),
        "after_instruments": int(buy_trades["code"].nunique()) if not buy_trades.empty else 0,
        "dates": int(result["equity"]["date"].nunique()) if not result["equity"].empty else 0,
        "reason": (
            "before=调仓日买入候选，after=实际买入成交（卖出另列于成交记录）；"
            "信号在收盘后冻结，下一完整交易日开盘撮合；按计划持仓数分配，"
            f"单票≤{float(fees['max_position_pct'])*100:g}%、总仓≤{float(fees['invest_ratio'])*100:g}%，候选不足保留现金；"
            "含 T+1、整手、涨跌停、停牌、费用、滑点和容量限制"
            + ("；门控关闭后下一完整交易日优先退出，失败则逐日重试" if active_gate_info else "")
        ),
        "combine": "NEXT_OPEN",
    })
    pipeline_dict = asdict(pipeline)
    pipeline_hash = _pipeline_hash(pipeline)
    result["pipeline"] = pipeline_dict
    result["pipeline_name"] = pipeline.name
    result["strategy"] = {
        "id": pipeline.id, "name": pipeline.name, "category": "乐高组合流水线",
        "summary": pipeline.description,
        "data_requirement": "本地全市场日线；可选积木所需数据必须通过可用性校验",
        "default_params": {}, "source_name": "睿盘乐高回测引擎", "source_url": "",
        "lesson": "查看逐层沙漏、分项因子贡献和成交记录，先确认无未来函数再解释收益。",
    }
    result["stage_funnel"] = pd.DataFrame(funnel_rows)
    result["rank_details"] = rank_details
    result["factor_analysis"] = factor_analysis
    result["factor_groups"] = factor_groups
    result["factor_correlations"] = factor_correlations
    result["config"].update({
        "pipeline_id": pipeline.id, "pipeline_name": pipeline.name,
        "pipeline_hash": pipeline_hash,
        "universe_scope": scope, "top_k": top_k, "positions": positions,
        "rebalance": rebalance,
    })
    result["coverage"].update({
        "pipeline": True, "eligible_rows": int(eligible["rows"]),
        "rank_detail_rows": int(len(rank_details)), "rank_top_k": top_k,
        "daily_rank_rows": int(len(rank_details)),
        "daily_rank_instruments": int(rank_details["instrument_id"].nunique()),
        "portfolio_positions": positions,
        "point_in_time_prices": True,
        "point_in_time_fundamentals": False,
        "historical_st_status": False,
        "active_market_gate": active_gate_info,
        "pipeline_hash": pipeline_hash,
        "feature_source": feature_source,
        "feature_fallback_reason": feature_fallback_reason,
    })
    result["timings"] = {
        "feature_seconds": round(feature_seconds, 6),
        "funnel_seconds": round(funnel_seconds, 6),
        "score_seconds": round(score_seconds, 6),
        "research_seconds": round(research_seconds, 6),
        "replay_seconds": round(replay_seconds, 6),
        "total_seconds": round(time.perf_counter() - total_started, 6),
    }
    result["cache_info"] = {
        "status": "miss" if cache_key else "bypass",
        "cache_key": cache_key,
        "dataset_version": int(dataset.get("dataset_version") or 0),
        "feature_source": feature_source,
        "fallback_reason": feature_fallback_reason,
        "source_run_id": None,
    }
    if persist:
        if cache_key:
            try:
                store_cached_result(result, cache_key)
                return create_cached_run(result, cache_key, False)
            except Exception as exc:
                result["cache_info"].update({"status": "bypass", "fallback_reason": f"cache-write-error:{exc}"})
        persist_result(result)
    return result


def _safe_pipeline_id(pipeline_id: str) -> str:
    safe_id = Path(str(pipeline_id)).name
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", safe_id):
        raise ValueError("流水线 id 无效")
    return safe_id


def _pipeline_summary(payload: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    runnable = True
    validation_error = ""
    try:
        validate_pipeline(payload)
    except Exception as exc:
        runnable = False
        validation_error = str(exc)
    return {
        "id": str(payload.get("id", path.stem if path else "")),
        "name": str(payload.get("name", path.stem if path else "")),
        "description": str(payload.get("description", "")),
        "version": int(payload.get("version", 1)),
        "revision": int(payload.get("revision", 1)),
        "scope": str(payload.get("scope", "all")),
        "start": str(payload.get("start", "")),
        "end": str(payload.get("end", "")),
        "blocks": len(payload.get("blocks", [])),
        "pipeline_hash": str(payload.get("pipeline_hash", "")),
        "updated_at": str(payload.get("updated_at", "")),
        "runnable": runnable,
        "validation_error": validation_error,
    }


def save_pipeline(spec: PipelineSpec | dict[str, Any], name: str) -> dict[str, Any]:
    pipeline = validate_pipeline_draft(spec)
    clean_name = str(name or pipeline.name).strip()
    if not clean_name:
        raise ValueError("流水线名称不能为空")
    pipeline_id = pipeline.id if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", pipeline.id) else f"pipeline-{uuid.uuid4().hex[:10]}"
    PIPELINES_DIR.mkdir(parents=True, exist_ok=True)
    path = PIPELINES_DIR / f"{pipeline_id}.json"
    existing: dict[str, Any] | None = None
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            existing = None
    current_revision = int((existing or {}).get("revision") or (1 if existing else 0))
    version_dir = PIPELINE_VERSIONS_DIR / pipeline_id
    version_dir.mkdir(parents=True, exist_ok=True)
    if existing and not (version_dir / f"{current_revision:06d}.json").exists():
        archived = copy.deepcopy(existing)
        archived["revision"] = current_revision
        (version_dir / f"{current_revision:06d}.json").write_text(
            json.dumps(archived, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    revision = current_revision + 1
    updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    payload = asdict(pipeline)
    payload.update({
        "id": pipeline_id, "name": clean_name,
        "pipeline_hash": _pipeline_hash(pipeline),
        "revision": revision,
        "updated_at": updated_at,
    })
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
    version_path = version_dir / f"{revision:06d}.json"
    version_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**_pipeline_summary(payload, path), "path": str(path)}


def list_pipelines() -> list[dict[str, Any]]:
    if not PIPELINES_DIR.exists():
        return []
    rows = []
    for path in sorted(PIPELINES_DIR.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows.append(_pipeline_summary(payload, path))
        except Exception:
            continue
    return rows


def load_pipeline(pipeline_id: str) -> PipelineSpec:
    safe_id = _safe_pipeline_id(pipeline_id)
    path = PIPELINES_DIR / f"{safe_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"找不到流水线: {safe_id}")
    return validate_pipeline_draft(json.loads(path.read_text(encoding="utf-8")))


def list_pipeline_versions(pipeline_id: str) -> list[dict[str, Any]]:
    safe_id = _safe_pipeline_id(pipeline_id)
    version_dir = PIPELINE_VERSIONS_DIR / safe_id
    if not version_dir.exists():
        return []
    rows = []
    for path in sorted(version_dir.glob("*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows.append(_pipeline_summary(payload, path))
        except Exception:
            continue
    return rows


def load_pipeline_version(pipeline_id: str, revision: int) -> PipelineSpec:
    safe_id = _safe_pipeline_id(pipeline_id)
    if revision < 1:
        raise ValueError("历史版本号必须大于 0")
    path = PIPELINE_VERSIONS_DIR / safe_id / f"{revision:06d}.json"
    if not path.exists():
        raise FileNotFoundError(f"找不到流水线 {safe_id} 的版本 {revision}")
    return validate_pipeline_draft(json.loads(path.read_text(encoding="utf-8")))


def restore_pipeline_version(pipeline_id: str, revision: int) -> dict[str, Any]:
    pipeline = load_pipeline_version(pipeline_id, revision)
    payload = asdict(pipeline)
    payload["id"] = _safe_pipeline_id(pipeline_id)
    return save_pipeline(payload, pipeline.name)


def delete_pipeline(pipeline_id: str) -> dict[str, Any]:
    safe_id = _safe_pipeline_id(pipeline_id)
    path = PIPELINES_DIR / f"{safe_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"找不到流水线: {safe_id}")
    deleted_at = datetime.now().strftime("%Y%m%d-%H%M%S")
    trash_dir = PRODUCT_WORK_DIR / "pipeline-trash" / f"{safe_id}-{deleted_at}"
    trash_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(trash_dir / path.name))
    version_dir = PIPELINE_VERSIONS_DIR / safe_id
    if version_dir.exists():
        shutil.move(str(version_dir), str(trash_dir / "versions"))
    return {"ok": True, "id": safe_id, "deleted_at": deleted_at}


def list_custom_block_templates() -> list[dict[str, Any]]:
    if not CUSTOM_BLOCKS_FILE.exists():
        return []
    try:
        payload = json.loads(CUSTOM_BLOCKS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = []
    for item in payload if isinstance(payload, list) else []:
        base_type = str(item.get("base_type") or "")
        template_id = str(item.get("id") or "")
        if base_type not in _CUSTOM_FORMULA_BLOCKS:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", template_id):
            continue
        rows.append({
            "id": template_id,
            "base_type": base_type,
            "category": BLOCK_CATALOG[base_type]["category"],
            "name": str(item.get("name") or BLOCK_CATALOG[base_type]["name"])[:40],
            "description": str(item.get("description") or "固定自定义模块")[:200],
            "params": dict(item.get("params") or {}),
            "updated_at": str(item.get("updated_at") or ""),
        })
    return rows


def catalog_with_custom_templates() -> dict[str, dict[str, Any]]:
    catalog = {key: {**value, "params": dict(value.get("params") or {})}
               for key, value in BLOCK_CATALOG.items()}
    for item in list_custom_block_templates():
        catalog_key = f"user_template_{item['id']}"
        catalog[catalog_key] = {
            "category": item["category"],
            "name": item["name"],
            "description": item["description"],
            "params": dict(item["params"]),
            "available": True,
            "disabled_reason": "",
            "required_data": [],
            "base_type": item["base_type"],
            "template_id": item["id"],
            "user_template": True,
        }
    return catalog


def save_custom_block_template(
    block: dict[str, Any], template_id: str | None = None,
    description: str = "",
) -> dict[str, Any]:
    raw = dict(block or {})
    base_type = str(raw.get("type") or "")
    if base_type not in _CUSTOM_FORMULA_BLOCKS:
        raise ValueError("只有自定义筛选、策略、因子和风险惩罚节点可以保存到左侧节点库")
    params = dict(BLOCK_CATALOG[base_type].get("params") or {})
    params.update(dict(raw.get("params") or {}))
    name = str(raw.get("name") or params.get("name") or "").strip()
    if not 1 <= len(name) <= 40:
        raise ValueError("固定模块名称长度必须为 1 到 40 个字符")
    formula = str(params.get("formula") or "")
    _compile_custom_formula(formula, boolean=_CUSTOM_FORMULA_BLOCKS[base_type])
    if base_type == "factor_custom_formula":
        weight = float(params.get("weight", 0))
        if not math.isfinite(weight) or weight < 0:
            raise ValueError("自定义因子权重不能为负")
        if params.get("direction", "higher") not in {"higher", "lower"}:
            raise ValueError("自定义因子方向只能是 higher 或 lower")
    if base_type == "penalty_custom_formula":
        weight = float(params.get("weight", 0))
        if not math.isfinite(weight) or weight < 0:
            raise ValueError("自定义风险惩罚权重不能为负")
    if base_type in {"factor_custom_formula", "penalty_custom_formula"}:
        if params.get("method", "percentile") not in {"robust_sigmoid", "percentile"}:
            raise ValueError("固定模块标准化方式无效")
    safe_id = str(template_id or "")
    if safe_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", safe_id):
        raise ValueError("固定模块 id 无效")
    safe_id = safe_id or f"custom-{uuid.uuid4().hex[:12]}"
    params["name"] = name
    if base_type in {"filter_custom_formula", "custom_formula"}:
        params["group"] = f"user_{safe_id}"
    row = {
        "id": safe_id,
        "base_type": base_type,
        "category": BLOCK_CATALOG[base_type]["category"],
        "name": name,
        "description": str(description or f"固定模块 · {formula}").strip()[:200],
        "params": params,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    rows = [item for item in list_custom_block_templates() if item["id"] != safe_id]
    rows.append(row)
    CUSTOM_BLOCKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = CUSTOM_BLOCKS_FILE.with_suffix(".json.tmp")
    temp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(CUSTOM_BLOCKS_FILE)
    catalog_key = f"user_template_{safe_id}"
    meta = catalog_with_custom_templates()[catalog_key]
    return {"template": row, "catalog_key": catalog_key, "meta": meta}


__all__ = [
    "BLOCK_CATALOG", "FACTOR_REGISTRY", "BlockSpec", "PipelineSpec", "default_pipeline",
    "standard_multifactor_pipeline", "blank_pipeline",
    "validate_pipeline_draft", "validate_pipeline", "run_lego_backtest", "save_pipeline",
    "list_pipelines", "load_pipeline", "list_pipeline_versions", "load_pipeline_version",
    "restore_pipeline_version", "delete_pipeline", "catalog_with_custom_templates",
    "list_custom_block_templates", "save_custom_block_template",
]
