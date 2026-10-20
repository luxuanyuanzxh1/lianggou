from __future__ import annotations

import tempfile
import sys
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from backtest_lab.config import DEFAULT_CODES, MINUTE_DB
from backtest_lab.data import database_summary, import_bundled_minutes, minute_coverage, resolve_codes, search_instruments
from backtest_lab.engine import BacktestConfig, list_runs, load_run, run_backtest
from backtest_lab.minute_fetcher import fetch_many
from backtest_lab.strategies import STRATEGIES
from backtest_lab.veighna_adapter import framework_status


st.set_page_config(page_title="量构 QuantWeave 回测实验室", page_icon="📈", layout="wide", initial_sidebar_state="expanded")
st.markdown(
    """
    <style>
    .stApp{background:#0a1014;color:#d7e0e5}.block-container{padding-top:1.25rem;max-width:1600px}
    [data-testid="stSidebar"]{background:#10181d;border-right:1px solid #293842}
    [data-testid="stMetric"]{background:#111b21;border:1px solid #293a44;padding:12px}
    .safe{padding:9px 11px;border:1px solid #2f6e54;background:#10251b;color:#78d6aa;font-weight:700}
    .method{padding:10px 12px;border-left:3px solid #4f91b7;background:#101c24;color:#9eb7c6;line-height:1.7}
    .warning{padding:9px 11px;border:1px solid #72512b;background:#281e13;color:#e0b16e}
    .oss{padding:10px 12px;border:1px solid #2b3d48;background:#101a20;margin-bottom:7px}
    .oss b,.oss span{display:block}.oss span{margin-top:4px;color:#7f929e;font-size:12px}
    h1,h2,h3{letter-spacing:0}.stButton button{border-radius:4px}
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(ttl=60)
def summary():
    return database_summary()


def codes_from_text(value: str) -> list[str]:
    return [item.strip().split(".")[0].zfill(6) for item in value.replace("，", ",").split(",") if item.strip()]


def metric(value, kind="pct"):
    if value is None:
        return "--"
    if kind == "pct":
        return f"{float(value):.2%}"
    if kind == "money":
        return f"¥{float(value):,.2f}"
    return f"{float(value):,.2f}"


def equity_candles(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    """Convert one end-of-day equity series into daily net-value candles.

    Backtests currently persist one point per close.  The next candle opens at
    the preceding close; high/low are therefore the envelope of that open and
    the current close.  This is a truthful daily net-value K-line, not invented
    intraday portfolio volatility.
    """

    values = frame[["date", column]].dropna().copy()
    values["open"] = values[column].shift(1).fillna(values[column])
    values["close"] = values[column]
    values["high"] = values[["open", "close"]].max(axis=1)
    values["low"] = values[["open", "close"]].min(axis=1)
    return values


def render_equity_kline(frame: pd.DataFrame, column: str, title: str) -> None:
    candles = equity_candles(frame, column)
    figure = go.Figure(go.Candlestick(
        x=candles["date"],
        open=candles["open"],
        high=candles["high"],
        low=candles["low"],
        close=candles["close"],
        increasing_line_color="#ef5b5b",
        increasing_fillcolor="#ef5b5b",
        decreasing_line_color="#20b47a",
        decreasing_fillcolor="#10181d",
        name=title,
    ))
    figure.update_layout(
        height=430,
        margin=dict(l=10, r=10, t=30, b=10),
        paper_bgcolor="#10181d",
        plot_bgcolor="#10181d",
        title=title,
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        yaxis_title="净值",
    )
    st.plotly_chart(figure, use_container_width=True)


def render_result(result: dict):
    metrics = result.get("metrics", {})
    strategy = result.get("strategy")
    strategy_name = result.get("pipeline_name") or (
        strategy.name if hasattr(strategy, "name") else strategy.get("name", result["config"]["strategy"])
    )
    st.subheader(f"{strategy_name} · 回测结果")
    columns = st.columns(8)
    values = [
        ("累计收益", metric(metrics.get("total_return"))),
        ("年化收益", metric(metrics.get("cagr"))),
        ("最大回撤", metric(metrics.get("max_drawdown"))),
        ("夏普比率", metric(metrics.get("sharpe"), "number")),
        ("年化波动", metric(metrics.get("annual_volatility"))),
        ("胜率", metric(metrics.get("win_rate"))),
        ("成交笔数", str(metrics.get("trades", 0))),
        ("期末权益", metric(metrics.get("final_equity"), "money")),
    ]
    for column, (label, value) in zip(columns, values):
        column.metric(label, value)
    coverage = result.get("coverage", {})
    if coverage.get("universe_size"):
        st.markdown(
            f'<div class="method">全市场回测：目录股票 {coverage.get("universe_size", 0):,} 只，'
            f'硬过滤后可参与 {coverage.get("eligible_universe_size", coverage.get("universe_size", 0)):,} 只；'
            f'实际调仓候选 {coverage.get("signal_instruments", 0):,} 只 / {coverage.get("candidate_rows", 0):,} 条，'
            f'每日 Top N 排名记录 {coverage.get("daily_rank_rows", coverage.get("candidate_rows", 0)):,} 条，'
            f'完整交易日 {coverage.get("dates", 0):,} 个。只把调仓候选及已有持仓交给撮合层。</div>',
            unsafe_allow_html=True,
        )
        if coverage.get("price_adjustment") == "unadjusted" or coverage.get("point_in_time_universe") is False:
            st.markdown(
                '<div class="warning">数据边界：当前全市场库尚未补齐全历史复权因子、历史退市股票和历史 ST 状态。'
                '本结果可用于验证策略流水线和撮合逻辑；在补齐 point-in-time 数据前，不应把收益数字当作正式策略结论。</div>',
                unsafe_allow_html=True,
            )
    if result["config"].get("execution_mode") in {"next_1445", "same_day_1445"}:
        same_day = result["config"].get("execution_mode") == "same_day_1445"
        method = "当日 14:45 冻结信号，下一根分钟 K 线开盘成交" if same_day else "前一交易日收盘后形成信号，下一交易日 14:45 附近成交"
        st.markdown(
            f'<div class="method">成交定义：{method}。'
            f'分钟覆盖率 {coverage.get("minute_coverage", 0):.1%}，共同有效交易日 {coverage.get("dates", 0)}。'
            "缺分钟线的日期不会用日开盘价偷偷替代。</div>",
            unsafe_allow_html=True,
        )
    equity = result.get("equity", pd.DataFrame()).copy()
    if not equity.empty:
        equity["date"] = pd.to_datetime(equity["date"])
        strategy_tab, benchmark_tab = st.tabs(["策略净值 K 线", "股票池基准 K 线"])
        with strategy_tab:
            render_equity_kline(equity, "equity", "策略净值日 K 线")
        with benchmark_tab:
            render_equity_kline(equity, "benchmark", "股票池等权基准日 K 线")
        drawdown = go.Figure(go.Scatter(x=equity["date"], y=equity["drawdown"] * 100, fill="tozeroy", line=dict(color="#d16d6d"), name="回撤"))
        drawdown.update_layout(height=220, margin=dict(l=10, r=10, t=20, b=10), paper_bgcolor="#10181d", plot_bgcolor="#10181d", yaxis_title="回撤 %")
        st.plotly_chart(drawdown, use_container_width=True)
    trades = result.get("trades", pd.DataFrame()).copy()
    st.subheader("完整成交记录")
    if trades.empty:
        st.info("该参数范围内没有产生已成交订单。")
    else:
        display = trades.rename(columns={"date": "日期", "symbol": "股票", "side": "方向", "price": "成交价", "shares": "数量", "amount": "成交金额", "fee": "费用", "realized_pnl": "单笔盈亏", "realized_return": "单笔收益率", "reason": "原因"})
        display["单笔收益率"] = display["单笔收益率"].map(lambda value: "--" if pd.isna(value) else f"{value:.2%}")
        st.dataframe(display[["日期", "股票", "方向", "成交价", "数量", "成交金额", "费用", "单笔盈亏", "单笔收益率", "原因"]], hide_index=True, use_container_width=True, height=420)
        st.download_button("下载成交 CSV", trades.to_csv(index=False).encode("utf-8-sig"), f"{result['run_id']}-trades.csv", "text/csv")
    if not equity.empty:
        col1, col2 = st.columns(2)
        col1.download_button("下载权益 CSV", equity.to_csv(index=False).encode("utf-8-sig"), f"{result['run_id']}-equity.csv", "text/csv")
        if col2.button("生成 QuantStats HTML 报告", use_container_width=True):
            try:
                import quantstats as qs

                returns = equity.set_index("date")["equity"].pct_change().dropna()
                with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as handle:
                    path = Path(handle.name)
                qs.reports.html(returns, output=str(path), title=f"量构 QuantWeave {strategy_name}")
                st.session_state["quantstats_html"] = path.read_bytes()
                path.unlink(missing_ok=True)
            except Exception as exc:
                st.error(f"报告生成失败: {exc}")
        if st.session_state.get("quantstats_html"):
            col2.download_button("下载 QuantStats 报告", st.session_state["quantstats_html"], f"{result['run_id']}-quantstats.html", "text/html", use_container_width=True)


db = summary()
veighna = framework_status()
with st.sidebar:
    st.markdown("## 量构 QuantWeave 回测实验室")
    st.markdown('<div class="safe">只读本地行情 · 只做历史回测</div>', unsafe_allow_html=True)
    pages = ["节点工作站", "经典回测", "历史结果", "分钟数据", "策略课堂", "开源组件"]
    requested_page = str(st.query_params.get("page", ""))
    default_page = 0 if requested_page in {"", "lego"} else pages.index("经典回测")
    page = st.radio("导航", pages, index=default_page)
    st.caption(f"VeighNa {veighna['vnpy']} · Backtrader · QuantStats")
    st.caption(f"日线 {db['daily_rows']:,} 条")
    st.caption(f"范围 {db['min_date']} 至 {db['max_date']}")

if page != "节点工作站":
    st.title(page)

if page == "节点工作站":
    from backtest_lab.lego_ui import render_lego_page

    render_lego_page(db, render_result)

elif page == "经典回测":
    left, right = st.columns([0.9, 1.45])
    with left:
        strategy_id = st.selectbox("策略", list(STRATEGIES), format_func=lambda key: f"{STRATEGIES[key].name}｜{STRATEGIES[key].category}")
        info = STRATEGIES[strategy_id]
        st.caption(info.summary)
        pool_mode = st.selectbox("股票池范围", ["沪深京全市场", "沪深全市场", "自定义代码"])
        scope = {"沪深京全市场": "all", "沪深全市场": "sh_sz", "自定义代码": "custom"}[pool_mode]
        if scope == "custom":
            codes_text = st.text_area("股票池（代码逗号分隔）", ",".join(DEFAULT_CODES), height=82)
        else:
            codes_text = ""
            st.caption("全市场先在 DuckDB 中向量化扫描，再把每日排名靠前的股票交给事件撮合层。")
        start_default = max(pd.Timestamp(db["min_date"]).date(), pd.Timestamp(db["max_date"]).date() - timedelta(days=730))
        dates = st.date_input("回测日期", value=(start_default, pd.Timestamp(db["max_date"]).date()), min_value=pd.Timestamp(db["min_date"]).date(), max_value=pd.Timestamp(db["max_date"]).date())
        execution_options = ["次日开盘（日线全历史）"] if scope != "custom" else ["次日开盘（日线全历史）", "次日14:45（分钟线精确模式）", "当日14:45冻结信号→下一根分钟K成交"]
        execution_label = st.selectbox(
            "成交时点",
            execution_options,
            index=2 if scope == "custom" and strategy_id == "tail_strength_1445" else 0,
            key=f"execution-{scope}-{strategy_id}",
        )
        execution_mode = "same_day_1445" if execution_label.startswith("当日") else "next_1445" if "次日14:45" in execution_label else "next_open"
        frequency = st.selectbox("分钟周期", ["5m", "1m", "15m"], disabled=execution_mode == "next_open")
        initial_cash = st.number_input("初始资金", min_value=100_000.0, value=1_000_000.0, step=100_000.0)
        if scope != "custom":
            full_top_n = st.number_input("组合持仓数量 Top N", min_value=1, max_value=100, value=20, step=1)
            full_rebalance = st.number_input("全市场调仓间隔（交易日）", min_value=1, max_value=60, value=5, step=1)
        else:
            full_top_n, full_rebalance = 20, 5
        st.markdown("**策略参数**")
        params = dict(info.default_params)
        for key, value in list(params.items()):
            if scope != "custom" and key in {"top_n", "rebalance"}:
                params[key] = int(full_top_n if key == "top_n" else full_rebalance)
                continue
            label = {"fast": "短均线", "slow": "长均线", "period": "RSI周期", "buy_level": "买入阈值", "sell_level": "卖出阈值", "lookback": "动量周期", "top_n": "持仓数量", "rebalance": "调仓间隔", "entry": "突破周期", "exit": "退出周期", "train_window": "训练窗口", "retrain": "重训间隔", "threshold": "预测阈值"}.get(key, key)
            if isinstance(value, float):
                params[key] = st.number_input(label, value=float(value), step=0.01, key=f"param-{strategy_id}-{key}")
            else:
                params[key] = st.number_input(label, min_value=1, value=int(value), step=1, key=f"param-{strategy_id}-{key}")
        with st.expander("交易成本和约束"):
            commission = st.number_input("佣金率", min_value=0.0, value=0.0003, format="%.5f")
            stamp_tax = st.number_input("卖出印花税", min_value=0.0, value=0.0005, format="%.5f")
            slippage = st.number_input("基础滑点", min_value=0.0, value=0.0005, format="%.5f")
            participation = st.number_input("最大成交量参与率", min_value=0.01, max_value=1.0, value=0.10, step=0.01)
        full_supported = strategy_id in {"buy_hold", "ma_cross", "rsi", "momentum", "turtle"}
        if scope != "custom" and not full_supported:
            st.warning("该策略需要全市场分钟线或滚动机器学习任务，当前先选择日线五类策略运行全市场。")
        confirmed = st.checkbox("确认使用无未来函数的次日成交规则", value=True)
        run = st.button("开始全市场回测" if scope != "custom" else "开始回测", type="primary", disabled=not confirmed or (scope != "custom" and not full_supported), use_container_width=True)
    codes = codes_from_text(codes_text) if scope == "custom" else []
    resolved = pd.DataFrame(resolve_codes(codes)) if codes else pd.DataFrame()
    with right:
        st.subheader("数据与方法预检")
        if scope != "custom":
            pool_count = db["stocks"] if scope == "all" else "沪深股票"
            cols = st.columns(3)
            cols[0].metric("股票池", pool_mode)
            cols[1].metric("目录股票", f"{pool_count:,}" if isinstance(pool_count, int) else pool_count)
            cols[2].metric("历史日线", f"{db['daily_rows']:,}")
            lookup = st.text_input("在全市场目录中确认股票代码或名称", placeholder="例如：贵州茅台 / 600519")
            matches = pd.DataFrame(search_instruments(lookup, 20)) if lookup else pd.DataFrame()
            if not matches.empty:
                if scope == "sh_sz":
                    matches = matches[matches["exchange"].isin(["SH", "SZ"])]
                st.dataframe(matches, hide_index=True, use_container_width=True)
            st.info(f"运行时按每个交易日读取完整股票池，计算信号后只持有 Top {full_top_n}；不是只用今天仍上市的几只示例股。")
        elif resolved.empty:
            st.warning("股票代码未在本地数据库中找到。")
        else:
            st.dataframe(resolved[["name", "code", "asset_type", "exchange"]], hide_index=True, use_container_width=True)
        st.markdown(
            '<div class="method"><b>点时规则</b><br>日线策略只读取信号日收盘前已经完成的数据；订单在下一交易日成交。'
            "次日 14:45 模式使用前一日信号；同日模式在 14:45 冻结分钟信号并于下一根 K 线成交。买入后最早下一交易日卖出，因此满足 T+1。</div>",
            unsafe_allow_html=True,
        )
        st.caption("自动剔除名称含 ST 的证券；100 股整手；停牌、涨跌停和成交量参与率由撮合层约束。")
    if run:
        try:
            if not isinstance(dates, (tuple, list)) or len(dates) != 2:
                raise ValueError("请选择开始和结束日期")
            with st.spinner("正在从本地 DuckDB 读取并执行事件驱动回测…"):
                config = BacktestConfig(
                    strategy=strategy_id,
                    codes=codes,
                    start=dates[0],
                    end=dates[1],
                    universe_scope=scope,
                    initial_cash=initial_cash,
                    execution_mode=execution_mode,
                    minute_frequency=frequency,
                    commission=commission,
                    stamp_tax=stamp_tax,
                    slippage=slippage,
                    max_participation=participation,
                    params=params,
                )
                if scope == "custom":
                    result = run_backtest(config)
                else:
                    from backtest_lab.full_market import run_full_market_backtest

                    result = run_full_market_backtest(
                        config,
                        scope=scope,
                        top_n=int(full_top_n),
                        rebalance=int(full_rebalance),
                    )
            st.session_state["backtest_result"] = result
            st.success(f"回测完成并保存：{result['run_id']}")
        except Exception as exc:
            st.error(str(exc))
    if st.session_state.get("backtest_result"):
        render_result(st.session_state["backtest_result"])

elif page == "历史结果":
    runs = list_runs()
    if not runs:
        st.info("尚无保存的回测结果。")
    else:
        labels = {row["run_id"]: f"{row['run_id']} · {row['strategy']['name']} · {row['config']['start']}~{row['config']['end']}" for row in runs}
        selected = st.selectbox("已保存运行", list(labels), format_func=labels.get)
        if st.button("载入结果", type="primary"):
            st.session_state["loaded_result"] = load_run(selected)
        if st.session_state.get("loaded_result"):
            render_result(st.session_state["loaded_result"])

elif page == "分钟数据":
    st.markdown('<div class="warning">14:45 当日选股若使用 14:45 柱开盘价会产生未来函数。本系统严格使用“前日信号→次日14:45”，后续同日分钟策略会使用 14:45 冻结信号、14:46/下一根K线成交。</div>', unsafe_allow_html=True)
    import_col, status_col = st.columns([0.35, 0.65])
    with import_col:
        st.subheader("已有样本")
        st.write("项目内保存了 150 只股票、2026 年约半年 15 分钟样本，可直接导入独立分钟缓存。")
        if st.button("导入已有 15 分钟样本", use_container_width=True):
            with st.spinner("导入中…"):
                st.success(import_bundled_minutes())
            st.cache_data.clear()
    with status_col:
        st.subheader("当前覆盖")
        coverage = minute_coverage()
        if coverage.empty:
            st.info("分钟缓存尚为空。")
        else:
            st.dataframe(coverage, hide_index=True, use_container_width=True, height=230)
    st.subheader("从通达信公开行情节点回填")
    col1, col2, col3 = st.columns(3)
    fetch_codes = col1.text_area("股票代码", ",".join(DEFAULT_CODES), height=75)
    fetch_frequency = col2.selectbox("周期", ["5m", "1m", "15m"], key="fetch-frequency")
    fetch_dates = col3.date_input("范围", value=(max(date.today() - timedelta(days=365), pd.Timestamp(db["min_date"]).date()), min(date.today(), pd.Timestamp(db["max_date"]).date())), key="fetch-dates")
    st.caption("公共节点实测：1 分钟约近 4 个月；5/15 分钟约近 2 年。更早的精确分钟历史需要 QMT 历史权限或持牌商业数据源。")
    if st.button("抓取 14:30–15:00 窗口", type="primary"):
        if not isinstance(fetch_dates, (tuple, list)) or len(fetch_dates) != 2:
            st.error("请选择开始和结束日期")
        else:
            progress = st.progress(0.0)
            message = st.empty()
            def update(index, total, item):
                progress.progress(index / max(total, 1))
                message.write(f"{index}/{total} · {item.get('code')} · {item.get('rows', 0)} 行")
            results = fetch_many(codes_from_text(fetch_codes), fetch_dates[0], fetch_dates[1], fetch_frequency, progress=update)
            st.dataframe(pd.DataFrame(results), hide_index=True, use_container_width=True)
            st.cache_data.clear()

elif page == "策略课堂":
    st.markdown('<div class="method">学习顺序建议：买入持有 → 双均线 → RSI → 动量轮动 → 海龟突破 → 滚动机器学习。每一步都先看样本外收益、最大回撤、换手和成本，再谈参数优化。</div>', unsafe_allow_html=True)
    selected_id = st.selectbox("选择课程", list(STRATEGIES), format_func=lambda key: STRATEGIES[key].name)
    lesson = STRATEGIES[selected_id]
    st.subheader(lesson.name)
    st.write(lesson.summary)
    st.markdown(f"**数据要求：** {lesson.data_requirement}")
    st.markdown(f"**默认参数：** `{lesson.default_params}`")
    st.info(lesson.lesson)
    st.markdown(f"参考：[ {lesson.source_name} ]({lesson.source_url})")
    st.markdown("""
    ### 一次规范回测的检查表

    1. 先固定股票池和时间范围，保留未参与调参的样本外区间。
    2. 信号只能读取当时已发布的数据；财务数据还要按实际发布日期对齐。
    3. 信号和成交至少错开一个可执行时点，本系统日线默认次日成交。
    4. 写入佣金、最低佣金、印花税、滑点、停牌、涨跌停、T+1 和整手。
    5. 同时比较简单基准，报告回撤、波动、换手、胜率和每笔成交。
    6. 机器学习必须用时间序列滚动训练，绝不能随机打乱后再切训练集。
    """)

else:
    components = [
        ("VeighNa", "主研究框架与组合/Alpha 扩展 · MIT · 已接本地 DuckDB BarData 适配器", "https://github.com/vnpy/vnpy"),
        ("Backtrader", "当前透明撮合回放内核与教学策略 API · GPL-3.0 · 版本 1.9.78.123", "https://github.com/mementum/backtrader"),
        ("QuantStats", "绩效指标与 HTML 报告 · Apache-2.0 · 版本 0.0.81", "https://github.com/ranaroussi/quantstats"),
        ("Streamlit", "本地交互式回测面板 · Apache-2.0", "https://github.com/streamlit/streamlit"),
        ("DuckDB", "直接查询本地 A 股日线数据库 · MIT", "https://github.com/duckdb/duckdb"),
        ("PyTDX", "通达信公开节点分钟数据适配 · MIT", "https://github.com/rainx/pytdx"),
        ("Microsoft Qlib", "后续机器学习研究层候选 · MIT", "https://github.com/microsoft/qlib"),
    ]
    st.write("以下组件均已明确用途；不会直接接实盘，也不会上传本地行情或策略。")
    for name, description, url in components:
        st.markdown(f'<div class="oss"><b><a href="{url}" target="_blank">{name}</a></b><span>{description}</span></div>', unsafe_allow_html=True)
    st.markdown("""
    ### 为什么没有直接复制一个陌生 GitHub 面板

    公开仓库中不少“量化面板”只支持美股或加密货币，或许可证与 README 的商业限制互相冲突。这里采用成熟组件的官方 API 组合，围绕 A 股本地研究流程组织页面，本地数据不离开电脑；QuantStats 的完整 HTML 报告可以在每次回测后生成下载。
    """)
