"""策略测试：调 QuantWeave 回测接口，出可读的 HTML 测试报告。

用法
----
.. code-block:: bash

    # 主测区间（预设默认 3 年）
    .venv/bin/python scripts/strategy_test.py --tag main

    # 指定区间做稳健性测试
    .venv/bin/python scripts/strategy_test.py --tag oos --start 2019-01-01 --end 2023-10-09

    # 两段一起跑并合并成一份报告
    .venv/bin/python scripts/strategy_test.py --windows 2023-10-09:2026-10-09,2019-01-01:2023-10-09

产出
----
- ``work/strategy-tests/<run_id>.json``   原始回测结果（不裁剪）
- ``work/strategy-tests/report-<tag>.html`` 自包含报告（内联 SVG，不依赖外网）
"""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "http://127.0.0.1:8767"
OUT_DIR = ROOT / "work" / "strategy-tests"


# ---------------------------------------------------------------- API 调用

def _post(path: str, payload: dict, timeout: int = 600) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(
        API + path, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕开本机代理
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _get(path: str) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(API + path, timeout=60) as resp:
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------- 指标计算

def _dt(s: str) -> date:
    return datetime.fromisoformat(s.replace("Z", "")).date()


def derive(result: dict) -> dict:
    eq = result["equity"]
    dates = [_dt(p["date"]) for p in eq]
    equity = [p["equity"] for p in eq]
    bench = [p.get("benchmark") or 0 for p in eq]

    peak, dd = -1e18, []
    for v in equity:
        peak = max(peak, v)
        dd.append(v / peak - 1)

    # 年度收益（按自然年，首年以起始权益为基准）
    yearly: dict[int, dict] = {}
    start_cap = equity[0]
    for d, v, b in zip(dates, equity, bench):
        slot = yearly.setdefault(d.year, {"first": v, "last": v, "bench_first": b, "bench_last": b})
        slot["last"] = v
        slot["bench_last"] = b
    prev_cap, prev_bench = start_cap, bench[0] if bench else 0
    year_rows = []
    for year in sorted(yearly):
        slot = yearly[year]
        base = prev_cap if year == sorted(yearly)[0] else slot["first"]
        bbase = prev_bench if year == sorted(yearly)[0] else slot["bench_first"]
        year_rows.append(
            {
                "year": year,
                "return": slot["last"] / base - 1 if base else None,
                "benchmark": slot["bench_last"] / bbase - 1 if bbase else None,
            }
        )
        prev_cap, prev_bench = slot["last"], slot["bench_last"]

    # 月度收益
    monthly: dict[tuple[int, int], float] = {}
    prev = start_cap
    for d, v in zip(dates, equity):
        key = (d.year, d.month)
        if key not in monthly:
            monthly[key] = None
        monthly[key] = v / prev - 1 if prev else None
        prev = v

    closed = result.get("closed_trades") or []
    hold_days = [_dt(c["exit_date"]).toordinal() - date.fromisoformat(c["entry_date"]).toordinal() for c in closed]
    wins = [c["pnl"] for c in closed if c["pnl"] > 0]
    losses = [c["pnl"] for c in closed if c["pnl"] < 0]

    return {
        "dates": dates,
        "equity": equity,
        "benchmark": bench,
        "drawdown": dd,
        "yearly": year_rows,
        "monthly": monthly,
        "excess_final": (equity[-1] / bench[-1] - 1) if bench and bench[-1] else None,
        "hold_days_avg": (sum(hold_days) / len(hold_days)) if hold_days else None,
        "profit_loss_ratio": (sum(wins) / abs(sum(losses))) if losses else None,
        "best": max(closed, key=lambda c: c["pnl"]) if closed else None,
        "worst": min(closed, key=lambda c: c["pnl"]) if closed else None,
    }


# ---------------------------------------------------------------- SVG 图

def _line_svg(series: list[tuple[str, list[float], str]], width=980, height=300, pad=44) -> str:
    """多条归一化折线。series = [(label, values, color)]"""
    n = max(len(s[1]) for s in series)
    lo = min(min(s[1]) for s in series if s[1])
    hi = max(max(s[1]) for s in series if s[1])
    span = (hi - lo) or 1
    lo -= span * 0.06
    hi += span * 0.06
    span = hi - lo

    def x(i: int) -> float:
        return pad + i * (width - pad * 1.4) / max(n - 1, 1)

    def y(v: float) -> float:
        return height - pad - (v - lo) / span * (height - pad * 1.6)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'xmlns="http://www.w3.org/2000/svg" font-family="-apple-system,sans-serif" font-size="11">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
    ]
    for k in range(5):
        val = lo + span * k / 4
        yy = y(val)
        parts.append(
            f'<line x1="{pad}" y1="{yy:.1f}" x2="{width - pad * 0.4:.1f}" y2="{yy:.1f}" stroke="#e8ebf0" stroke-width="1"/>'
            f'<text x="{pad - 8}" y="{yy + 4:.1f}" text-anchor="end" fill="#8b93a1">{val:.0f}</text>'
        )
    for label, values, color in series:
        if not values:
            continue
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.8"/>')
    parts.append("</svg>")
    return "".join(parts)


def _area_svg(values: list[float], width=980, height=180, pad=40) -> str:
    n = len(values)
    lo = min(values + [0]) * 100
    span = (0 - lo) or 1

    def x(i: int) -> float:
        return pad + i * (width - pad * 1.4) / max(n - 1, 1)

    def y(v: float) -> float:
        return pad + (0 - v * 100) / span * (height - pad * 1.6)

    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
    area = f"M {x(0):.1f},{y(0):.1f} L {pts.replace(' ', ' L ')} L {x(n - 1):.1f},{y(0):.1f} Z"
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'xmlns="http://www.w3.org/2000/svg" font-family="-apple-system,sans-serif" font-size="11">'
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>'
        f'<line x1="{pad}" y1="{y(0):.1f}" x2="{width - pad * 0.4:.1f}" y2="{y(0):.1f}" stroke="#c8cedb"/>'
        f'<path d="{area}" fill="rgba(214,69,69,0.14)" stroke="#d64545" stroke-width="1.4"/>'
        f'<text x="{pad}" y="{y(lo) + 6:.1f}" fill="#8b93a1">{lo:.0f}%</text>'
        f"</svg>"
    )


def _bars_svg(rows: list[dict], width=980, height=200, pad=44) -> str:
    vals = [(r["year"], r["return"] or 0, r["benchmark"] or 0) for r in rows]
    lo = min([0] + [v[1] for v in vals] + [v[2] for v in vals]) * 100
    hi = max([0.01] + [v[1] for v in vals] + [v[2] for v in vals]) * 100
    span = (hi - lo) or 1
    zero = pad + (0 - lo) / span * (height - pad * 1.6)
    slot = (width - pad * 1.4) / max(len(vals), 1)

    def y(v: float) -> float:
        return pad + (hi - v) / span * (height - pad * 1.6)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'xmlns="http://www.w3.org/2000/svg" font-family="-apple-system,sans-serif" font-size="11">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        f'<line x1="{pad}" y1="{zero:.1f}" x2="{width - pad * 0.4:.1f}" y2="{zero:.1f}" stroke="#c8cedb"/>',
    ]
    for i, (year, ret, bm) in enumerate(vals):
        bx = pad + i * slot + slot * 0.18
        bw = slot * 0.28
        for j, (v, color) in enumerate(((ret, "#d64545"), (bm, "#9aa4b2"))):
            top, bot = min(y(v * 100), zero), max(y(v * 100), zero)
            parts.append(
                f'<rect x="{bx + j * (bw + 3):.1f}" y="{top:.1f}" width="{bw:.1f}" '
                f'height="{max(bot - top, 1):.1f}" fill="{color}" rx="2"/>'
                f'<text x="{bx + j * (bw + 3) + bw / 2:.1f}" y="{(top - 5) if v >= 0 else (bot + 13):.1f}" '
                f'text-anchor="middle" fill="#5b6474">{v * 100:+.0f}%</text>'
            )
        parts.append(
            f'<text x="{bx + bw + 2:.1f}" y="{height - 10}" text-anchor="middle" fill="#39404d">{year}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------- HTML

def fmt_pct(v, digits=2, sign=True):
    if v is None:
        return "—"
    return f"{v * 100:+.{digits}f}%" if sign else f"{v * 100:.{digits}f}%"


def render(report: dict) -> str:
    main = report["runs"][0]
    r, d, m = main["result"], main["derived"], main["result"]["metrics"]
    cov = r["coverage"]
    cfg = r["config"]

    norm_eq = [v / d["equity"][0] * 100 for v in d["equity"]]
    norm_bm = [v / d["benchmark"][0] * 100 for v in d["benchmark"]] if d["benchmark"] else []
    eq_svg = _line_svg(
        [("组合", norm_eq, "#d64545"), ("基准", norm_bm, "#9aa4b2")]
    )
    dd_svg = _area_svg(d["drawdown"])
    yr_svg = _bars_svg(d["yearly"])

    years = sorted({k[0] for k in d["monthly"]})
    months = list(range(1, 13))
    head_cells = "".join(f"<th>{mm}月</th>" for mm in months)
    body_rows = []
    for yy in years:
        cells = []
        for mm in months:
            v = d["monthly"].get((yy, mm))
            if v is None:
                cells.append('<td class="na">·</td>')
            else:
                cls = "pos" if v > 0 else "neg"
                cells.append(f'<td class="{cls}">{v * 100:+.1f}</td>')
        yrow = next((x for x in d["yearly"] if x["year"] == yy), None)
        total = fmt_pct(yrow["return"], 1) if yrow else "—"
        body_rows.append(f"<tr><th>{yy}</th>{''.join(cells)}<td class='total'>{total}</td></tr>")

    year_rows = "".join(
        f"<tr><td>{x['year']}</td><td class='{'pos' if (x['return'] or 0) > 0 else 'neg'}'>{fmt_pct(x['return'])}</td>"
        f"<td>{fmt_pct(x['benchmark'])}</td>"
        f"<td class='{'pos' if (x['return'] or 0) > (x['benchmark'] or 0) else 'neg'}'>"
        f"{fmt_pct((x['return'] or 0) - (x['benchmark'] or 0))}</td></tr>"
        for x in d["yearly"]
    )

    factors = r.get("factor_analysis") or []
    factor_rows = "".join(
        f"<tr><td>{f['name']}</td><td>{f.get('weight', 0):.2f}</td>"
        f"<td class='{'pos' if f.get('rank_ic_20', 0) > 0 else 'neg'}'>{f.get('rank_ic_20', 0):+.4f}</td>"
        f"<td>{f.get('icir_20', 0):+.3f}</td>"
        f"<td>{f.get('positive_rate_20', 0) * 100:.1f}%</td>"
        f"<td>{f.get('observations', 0):,}</td></tr>"
        for f in factors
    )

    pnl = sorted(r.get("stock_pnl") or [], key=lambda x: x["pnl"], reverse=True)
    top = pnl[:8]
    bottom = pnl[-8:][::-1]

    def pnl_rows(items):
        return "".join(
            f"<tr><td>{x['name']}</td><td>{x['ticker']}</td><td>{x['trades']}</td>"
            f"<td class='{'pos' if x['pnl'] > 0 else 'neg'}'>{x['pnl']:+,.0f}</td>"
            f"<td>{fmt_pct(x['realized_return'])}</td></tr>"
            for x in items
        )

    windows = "".join(
        f"<tr><td>{w['start']} ~ {w['end']}</td>"
        f"<td class='{'pos' if w['result']['metrics']['total_return'] > 0 else 'neg'}'>"
        f"{fmt_pct(w['result']['metrics']['total_return'])}</td>"
        f"<td>{fmt_pct(w['result']['metrics']['cagr'])}</td>"
        f"<td>{w['result']['metrics']['sharpe']:.2f}</td>"
        f"<td class='neg'>{fmt_pct(w['result']['metrics']['max_drawdown'])}</td>"
        f"<td>{fmt_pct(w['derived']['excess_final'])}</td>"
        f"<td>{w['result']['metrics']['trades']}</td></tr>"
        for w in report["runs"]
    )

    # 因子在两段区间的 RankIC 对照：看方向是否稳定
    factor_compare_html = ""
    runs = report["runs"]
    if len(runs) > 1:
        per_run = [{f["name"]: f for f in (w["result"].get("factor_analysis") or [])} for w in runs]
        names = list(per_run[0].keys())
        rows = []
        for name in names:
            cells = []
            signs = []
            for table in per_run:
                v = (table.get(name) or {}).get("rank_ic_20") or 0
                cells.append(f"<td class='{'pos' if v > 0 else 'neg'}'>{v:+.4f}</td>")
                signs.append(v)
            w0 = (per_run[0].get(name) or {}).get("weight", 0)
            stable = "方向一致" if all(v > 0 for v in signs) or all(v < 0 for v in signs) else "方向漂移"
            conflict = "⚠ 与权重方向相反" if (w0 > 0 and all(v < 0 for v in signs)) else ""
            rows.append(
                f"<tr><td>{name}</td><td>{w0:.2f}</td>{''.join(cells)}"
                f"<td>{stable}</td><td class='{'neg' if conflict else ''}'>{conflict}</td></tr>"
            )
        heads = "".join(f"<th>{w['start'][:7]} ~ {w['end'][:7]}</th>" for w in runs)
        factor_compare_html = (
            '<h2>八之二、因子方向是否稳定</h2><div class="card">'
            f"<table><thead><tr><th>因子</th><th>权重</th>{heads}<th>跨区间</th><th>诊断</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
            '<div class="sub">RankIC 为负表示「因子值越大、未来 20 日相对收益越低」。'
            "配置为正向但两段 IC 都为负，等于长期做反向，必须反向使用或剔除。</div></div>"
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>策略测试报告 · {r['strategy']['name']}</title>
<style>
:root{{--ink:#1b1f27;--muted:#6b7382;--line:#e6e9f0;--bg:#f7f8fa;--card:#fff;--up:#d64545;--down:#1f9d55;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.65 -apple-system,"PingFang SC","Helvetica Neue",sans-serif}}
.wrap{{max-width:1080px;margin:0 auto;padding:36px 28px 80px}}
h1{{font-size:26px;margin:0 0 6px;letter-spacing:-.3px}}
h2{{font-size:17px;margin:34px 0 12px;padding-left:10px;border-left:3px solid #2f5bd7}}
.sub{{color:var(--muted);font-size:13px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin-top:14px}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:16px}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}}
.kpi .k{{color:var(--muted);font-size:12px}}
.kpi .v{{font-size:22px;font-weight:600;margin-top:2px;letter-spacing:-.5px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:7px 9px;text-align:right;border-bottom:1px solid var(--line)}}
thead th{{color:var(--muted);font-weight:500;font-size:12px}}
th:first-child,td:first-child{{text-align:left}}
tbody th{{text-align:left;font-weight:500}}
.pos{{color:var(--up)}} .neg{{color:var(--down)}} .na{{color:#c2c8d2}}
.total{{font-weight:600}}
.verdict{{background:#fff8f0;border:1px solid #f3d9bd;border-radius:10px;padding:14px 18px;margin-top:16px}}
.verdict b{{color:#b45309}}
ul{{margin:8px 0 0;padding-left:20px}} li{{margin:4px 0}}
.legend span{{display:inline-block;margin-right:16px;color:var(--muted);font-size:12px}}
.dot{{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:middle}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
code{{background:#f0f2f6;padding:1px 6px;border-radius:4px;font-size:12px}}
</style></head><body><div class="wrap">

<h1>{r['strategy']['name']}</h1>
<div class="sub">策略测试报告　·　{r['strategy']['summary']}<br>
数据源：本地主库 <code>主数据底座.duckdb</code>（raw 未复权，截至 {cov.get('last_date', cfg['end'])}）　·　
区间 {cfg['start']} ~ {cfg['end']}　·　run_id <code>{r['run_id']}</code>　·　生成于 {report['generated']}</div>

<div class="grid">
  <div class="kpi"><div class="k">总收益</div><div class="v {'pos-text' if m['total_return']>0 else ''}" style="color:{'#d64545' if m['total_return']>0 else '#1f9d55'}">{fmt_pct(m['total_return'])}</div></div>
  <div class="kpi"><div class="k">年化 CAGR</div><div class="v">{fmt_pct(m['cagr'])}</div></div>
  <div class="kpi"><div class="k">Sharpe</div><div class="v">{m['sharpe']:.2f}</div></div>
  <div class="kpi"><div class="k">最大回撤</div><div class="v neg">{fmt_pct(m['max_drawdown'])}</div></div>
  <div class="kpi"><div class="k">Calmar</div><div class="v">{m['calmar']:.2f}</div></div>
  <div class="kpi"><div class="k">年化波动</div><div class="v">{fmt_pct(m['annual_volatility'],1,False)}</div></div>
  <div class="kpi"><div class="k">胜率</div><div class="v">{fmt_pct(m['win_rate'],1,False)}</div></div>
  <div class="kpi"><div class="k">超额收益（对基准）</div><div class="v {'pos' if (d['excess_final'] or 0)>0 else 'neg'}">{fmt_pct(d['excess_final'])}</div></div>
</div>

<div class="verdict">
<b>一句话判定：</b>{report['verdict']}
</div>

<h2>一、净值与基准</h2>
<div class="card">
<div class="legend"><span><i class="dot" style="background:#d64545"></i>组合净值（起点=100）</span>
<span><i class="dot" style="background:#9aa4b2"></i>基准（市值加权 Top300 自建）</span></div>
{eq_svg}
<div class="sub">期末：组合 {d['equity'][-1]:,.0f} 元　基准 {d['benchmark'][-1]:,.0f} 元　
期初 {d['equity'][0]:,.0f} 元　|　持仓 {cov['instruments']} 只 / 候选池 {cov['eligible_universe_size']} 只 / 调仓 {cov['rebalance_events']} 次</div>
</div>

<h2>二、回撤</h2>
<div class="card">{dd_svg}
<div class="sub">最大回撤 {fmt_pct(m['max_drawdown'])}，恢复情况见上图谷底到期末的回升斜率。</div></div>

<h2>三、分年收益</h2>
<div class="card">{yr_svg}
<table style="margin-top:10px"><thead><tr><th>年度</th><th>组合</th><th>基准</th><th>超额</th></tr></thead>
<tbody>{year_rows}</tbody></table></div>

<h2>四、月度收益矩阵（%）</h2>
<div class="card"><table><thead><tr><th>年\\月</th>{head_cells}<th>全年</th></tr></thead>
<tbody>{''.join(body_rows)}</tbody></table></div>

<h2>五、因子有效性（横截面 RankIC）</h2>
<div class="card"><table><thead><tr><th>因子</th><th>权重</th><th>RankIC(20日)</th><th>ICIR</th><th>IC&gt;0 占比</th><th>样本数</th></tr></thead>
<tbody>{factor_rows}</tbody></table>
<div class="sub">RankIC 绝对值 &gt;0.03 且方向稳定才算有效；为负说明该因子在当前样本里反向，需反向使用或剔除。</div></div>

<h2>六、个股贡献</h2>
<div class="two">
<div class="card"><b>贡献最大</b><table><thead><tr><th>股票</th><th>代码</th><th>笔数</th><th>盈亏(元)</th><th>收益率</th></tr></thead><tbody>{pnl_rows(top)}</tbody></table></div>
<div class="card"><b>拖累最大</b><table><thead><tr><th>股票</th><th>代码</th><th>笔数</th><th>盈亏(元)</th><th>收益率</th></tr></thead><tbody>{pnl_rows(bottom)}</tbody></table></div>
</div>

<h2>七、交易统计</h2>
<div class="card"><table><thead><tr><th>项目</th><th>数值</th></tr></thead><tbody>
<tr><td>成交笔数（含开平）</td><td>{m['trades']}</td></tr>
<tr><td>平仓笔数</td><td>{m['closed_trades']}</td></tr>
<tr><td>平均持仓天数</td><td>{(d['hold_days_avg'] or 0):.1f} 天</td></tr>
<tr><td>盈亏比（总盈利/总亏损）</td><td>{(d['profit_loss_ratio'] or 0):.2f}</td></tr>
<tr><td>换手（年化，倍）</td><td>{m['turnover']:.1f}</td></tr>
<tr><td>单笔最佳 / 最差</td><td>{fmt_pct(d['best']['realized_return']) if d['best'] else '—'} / {fmt_pct(d['worst']['realized_return']) if d['worst'] else '—'}</td></tr>
<tr><td>交易成本设置</td><td>佣金 {cfg['commission']*10000:.1f}‱（最低 {cfg['min_commission']} 元）· 印花税 {cfg['stamp_tax']*10000:.0f}‱ · 滑点 {cfg['slippage']*10000:.0f}‱</td></tr>
</tbody></table></div>

<h2>八、分段稳健性</h2>
<div class="card"><table><thead><tr><th>区间</th><th>总收益</th><th>年化</th><th>Sharpe</th><th>最大回撤</th><th>对基准超额</th><th>成交</th></tr></thead>
<tbody>{windows}</tbody></table>
<div class="sub">两段独立区间同向才说明不是过拟合；仅一段有效要标注「样本内有效」。</div></div>

{factor_compare_html}

<h2>九、结论与风险</h2>
<div class="card"><ul>{report['risks_html']}</ul></div>

</div></body></html>"""


# ---------------------------------------------------------------- 主流程

def _verdict(runs: list[dict]) -> str:
    """判定必须看全部区间：单段好看但另一段崩，就是窗口依赖。"""
    main = runs[0]
    m, d = main["result"]["metrics"], main["derived"]
    sharpe, mdd = m["sharpe"], m["max_drawdown"]
    excess = d["excess_final"] or 0
    if sharpe >= 1.2 and abs(mdd) <= 0.25:
        tone = "风险调整后收益可接受"
    elif sharpe >= 0.8:
        tone = "收益能力有，但靠高波动换来"
    else:
        tone = "风险调整后不划算"
    direction = "跑赢基准" if excess > 0 else "跑输基准"
    head = (
        f"主区间（{main['start']} ~ {main['end']}）{tone}：年化 {fmt_pct(m['cagr'])}、"
        f"Sharpe {sharpe:.2f}、最大回撤 {fmt_pct(mdd)}，{direction} {fmt_pct(excess)}。"
    )

    others = runs[1:]
    if not others:
        return head + f"但只测了一段区间，未做样本外验证，不能当成稳健策略。"

    bad = [w for w in others if w["result"]["metrics"]["total_return"] < 0]
    if bad:
        detail = "；".join(
            f"{w['start']} ~ {w['end']} 收益 {fmt_pct(w['result']['metrics']['total_return'])}、"
            f"Sharpe {w['result']['metrics']['sharpe']:.2f}、回撤 {fmt_pct(w['result']['metrics']['max_drawdown'])}"
            for w in bad
        )
        return (
            head
            + f"<br><strong style='color:#b91c1c'>但换到独立区间直接失效：{detail}</strong>。"
            f"同一套规则在前一段亏钱、后一段大赚，说明收益来自「特定市场状态」而不是稳定 alpha，"
            f"不能直接上实盘——先用参数敏感性测试和逐因子拆解判断是哪一段在贡献。"
        )

    detail = "；".join(
        f"{w['start']} ~ {w['end']} {fmt_pct(w['result']['metrics']['total_return'])}"
        for w in others
    )
    return head + f"<br>其余区间同向：{detail}，跨区间方向一致，初步具备稳健性。"


def _risks(result: dict, derived: dict, runs: list[dict] | None = None) -> str:
    m = result["metrics"]
    items = []
    if runs and len(runs) > 1:
        spread = [w["result"]["metrics"]["cagr"] for w in runs]
        if min(spread) < 0 < max(spread):
            items.append(
                "<strong>区间依赖是第一风险</strong>：同一套规则在不同区间年化从 "
                + " 到 ".join(f"{v * 100:+.1f}%" for v in (min(spread), max(spread)))
                + "，跨牛熊不稳定。任何「历史年化」都不能外推到未来，仓位应按最差区间准备。"
            )
    if abs(m["max_drawdown"]) > 0.3:
        items.append(
            f"最大回撤 {fmt_pct(m['max_drawdown'])}，超过多数个人账户容忍度；实盘需预先设定敞口上限或加择时门控。"
        )
    if m["turnover"] > 30:
        items.append(
            f"年化换手 {m['turnover']:.0f} 倍，成本对结果高度敏感；把佣金/滑点上调一倍应重跑，确认收益是否被吃掉。"
        )
    items.append(
        "回测用<strong>未复权原始价</strong>（与服务器同口径）：除权日会被当成真实下跌，长周期会低估收益，"
        "短周期可能触发假止损。接本地库后 amount 为典型价派生（误差约 0.07%），仅影响流动性排序。"
    )
    items.append(
        "生存者偏差：本地库 instrument 只含当前在市标的，已退市股票不在样本内，历史收益偏乐观。"
    )
    items.append(
        "ST 判定用当前名称（静态），历史 ST 期间的 5% 涨跌停无法还原，涨跌停成交假设偏理想。"
    )
    factors = result.get("factor_analysis") or []
    neg = [f for f in factors if (f.get("rank_ic_20") or 0) < -0.02]
    if neg:
        names = "、".join(f["name"] for f in neg)
        items.append(f"因子 <strong>{names}</strong> 的 RankIC 为负且绝对值不小，说明方向与权重设定相反，建议反向或降权后再测。")
    return "".join(f"<li>{x}</li>" for x in items)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset", default="standard_multifactor")
    parser.add_argument("--windows", default=None, help="start:end,start:end")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--tag", default="main")
    parser.add_argument("--reuse", action="store_true", help="同区间已有结果就直接复用，不重复回测")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    catalog = _get("/api/v1/lego/catalog")
    pipeline = dict(catalog["presets"][args.preset])

    if args.windows:
        windows = [tuple(w.split(":")) for w in args.windows.split(",")]
    else:
        windows = [(args.start or pipeline["start"], args.end or pipeline["end"])]

    index_path = OUT_DIR / "index.json"
    index: dict[str, str] = {}
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8"))

    runs = []
    for start, end in windows:
        payload = dict(pipeline, start=start, end=end)
        key = f"{pipeline['id']}|{start}|{end}"
        cached = OUT_DIR / f"{index.get(key, '')}.json"
        if args.reuse and key in index and cached.exists():
            result = json.loads(cached.read_text(encoding="utf-8"))
            secs = 0.0
            print(f"↺ 复用 {start} ~ {end} ({result['run_id']})", flush=True)
        else:
            t0 = time.time()
            print(f"→ 回测 {start} ~ {end} ...", flush=True)
            result = _post("/api/v1/lego/backtests", {"pipeline": payload})
            secs = time.time() - t0
            (OUT_DIR / f"{result['run_id']}.json").write_text(
                json.dumps(result, ensure_ascii=False), encoding="utf-8"
            )
            index[key] = result["run_id"]
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
        runs.append(
            {
                "start": start,
                "end": end,
                "seconds": round(secs, 1),
                "result": result,
                "derived": derive(result),
            }
        )
        m = result["metrics"]
        print(
            f"  完成 {secs:.1f}s | 收益 {m['total_return']*100:+.2f}% | CAGR {m['cagr']*100:.2f}% | "
            f"Sharpe {m['sharpe']:.2f} | MDD {m['max_drawdown']*100:.2f}% | 成交 {m['trades']}",
            flush=True,
        )

    report = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "runs": runs,
        "verdict": _verdict(runs),
        "risks_html": _risks(runs[0]["result"], runs[0]["derived"], runs),
    }
    out = OUT_DIR / f"report-{args.tag}.html"
    out.write_text(render(report), encoding="utf-8")
    print(f"报告：{out}")


if __name__ == "__main__":
    main()
