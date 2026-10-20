"use client";

import { useEffect, useMemo, useState } from "react";
import type { ResearchContext } from "./research-context";

type FactorDefinition = {
  id: string;
  name: string;
  description: string;
  family: string;
  params: Record<string, unknown>;
};
type ModelDefinition = {
  id: string;
  name: string;
  available: boolean;
  kind: string;
  disabled_reason?: string;
};
type LabConfig = {
  name?: string;
  start_date?: string;
  end_date?: string;
  universe?: string;
  min_amount?: number;
  run_model?: boolean;
  model?: string;
  label_horizon?: number;
  train_window?: number;
  retrain_every?: number;
  research_pool?: number;
  min_train_dates?: number;
  max_train_rows?: number;
  positions?: number;
  rebalance_every?: number;
  factors?: string[];
  [key: string]: unknown;
};
type Catalog = {
  factors: FactorDefinition[];
  models: ModelDefinition[];
  defaults: LabConfig;
  safety: Record<string, string>;
};
type FactorSelection = { id: string; weight: number };
type CustomFactor = {
  name: string;
  formula: string;
  direction: "higher" | "lower";
  weight: number;
};
type HistoryRow = {
  id: string;
  createdAt: string;
  name: string;
  model: string;
  annual?: number;
  rankIc?: number;
};
type EquityPoint = { date?: string; trade_date?: string; value?: number; equity?: number };
type IcPoint = { date?: string; value?: number };
type GroupRow = {
  block_id?: string;
  name?: string;
  bucket?: number;
  forward_return_20?: number;
};
type CorrelationRow = { name_a?: string; name_b?: string; correlation?: number };
type FactorMetricRow = {
  block_id: string;
  name: string;
  direction?: string;
  weight?: number;
  rank_ic_5?: number;
  rank_ic_20?: number;
  icir_20?: number;
  positive_rate_20?: number;
  spread_20?: number;
  observations?: number;
};
type FeatureImportanceRow = { feature: string; importance?: number };
type TradeRow = {
  date?: string;
  code?: string;
  name?: string;
  side?: string;
  price?: number;
  shares?: number;
  amount?: number;
  value?: number;
  reason?: string;
};
type ResultMetrics = { annual_return?: number; cagr?: number; max_drawdown?: number };
type PortfolioResult = {
  metrics?: ResultMetrics;
  equity?: EquityPoint[];
  factor_analysis?: FactorMetricRow[];
  factor_groups?: GroupRow[];
  factor_correlations?: CorrelationRow[];
  trades?: TradeRow[];
};
type ModelSummary = {
  name?: string;
  fits?: number;
  predictions?: number;
  dates?: number;
  rank_ic?: number;
  icir?: number;
  positive_rate?: number;
  daily_ic?: IcPoint[];
  feature_importance?: FeatureImportanceRow[];
};
type SelectedFactorSummary = { block_id: string; name: string };
type FactorLabResult = {
  baseline?: PortfolioResult;
  model_result?: PortfolioResult | null;
  model_summary?: ModelSummary | null;
  selected_factors?: SelectedFactorSummary[];
  notes?: Record<string, unknown>;
};

const API = "";
const pct = (value: unknown) =>
  Number.isFinite(Number(value)) ? `${Number(value) >= 0 ? "+" : ""}${(Number(value) * 100).toFixed(2)}%` : "--";
const num = (value: unknown, digits = 3) =>
  Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "--";

function EquityChart({ baseline, model }: { baseline: EquityPoint[]; model: EquityPoint[] }) {
  const series = useMemo(() => {
    const normalize = (rows: EquityPoint[]) => {
      const clean = (rows || []).map(row => ({ date: String(row.date || row.trade_date || "").slice(0, 10), value: Number(row.value ?? row.equity) })).filter(row => row.date && Number.isFinite(row.value));
      const first = clean[0]?.value || 1;
      return clean.map(row => ({ ...row, value: row.value / first }));
    };
    return [{ name: "多因子基线", rows: normalize(baseline) }, { name: "机器学习", rows: normalize(model) }];
  }, [baseline, model]);
  const values = series.flatMap(item => item.rows.map(row => row.value));
  if (!values.length) return <div className="factor-lab-empty-chart">运行实验后显示净值对比</div>;
  const width = 900, height = 280, left = 54, right = 20, top = 22, bottom = 34;
  const low = Math.min(...values), high = Math.max(...values), range = Math.max(high - low, 0.01);
  const maxLength = Math.max(...series.map(item => item.rows.length), 1);
  const x = (index: number) => left + index / Math.max(maxLength - 1, 1) * (width - left - right);
  const y = (value: number) => top + (high - value) / range * (height - top - bottom);
  return <svg className="factor-lab-equity" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="多因子与机器学习样本外净值对比">
    {[0, .25, .5, .75, 1].map(level => { const value = high - range * level; const py = y(value); return <g key={level}><line x1={left} x2={width - right} y1={py} y2={py}/><text x="4" y={py + 4}>{value.toFixed(2)}</text></g>; })}
    {series.map((item, seriesIndex) => item.rows.length ? <polyline key={item.name} className={`series-${seriesIndex + 1}`} fill="none" points={item.rows.map((row, index) => `${x(index)},${y(row.value)}`).join(" ")}/> : null)}
    <text x={left} y={height - 8}>{series.find(item => item.rows.length)?.rows[0]?.date}</text>
    <text textAnchor="end" x={width - right} y={height - 8}>{series.find(item => item.rows.length)?.rows.at(-1)?.date}</text>
  </svg>;
}

function IcChart({ rows }: { rows: IcPoint[] }) {
  const clean = (rows || []).filter(row => Number.isFinite(Number(row.value))).slice(-180);
  if (!clean.length) return <div className="factor-lab-empty-chart">模型产生样本外预测后显示每日Rank IC</div>;
  const width = 900, height = 210, left = 46, right = 16, top = 16, bottom = 28;
  const bound = Math.max(.05, ...clean.map(row => Math.abs(Number(row.value))));
  const x = (index: number) => left + index / Math.max(clean.length - 1, 1) * (width - left - right);
  const y = (value: number) => top + (bound - value) / (bound * 2) * (height - top - bottom);
  return <svg className="factor-lab-ic" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="机器学习每日Rank IC">
    <line className="zero" x1={left} x2={width - right} y1={y(0)} y2={y(0)}/>
    <polyline fill="none" points={clean.map((row, index) => `${x(index)},${y(Number(row.value))}`).join(" ")}/>
    <text x="4" y={y(bound) + 4}>{bound.toFixed(2)}</text><text x="4" y={y(0) + 4}>0</text><text x="4" y={y(-bound) + 4}>{(-bound).toFixed(2)}</text>
  </svg>;
}

function GroupBars({ rows }: { rows: GroupRow[] }) {
  if (!rows?.length) return <div className="factor-lab-empty-chart">暂无分组收益</div>;
  const groups = Object.entries(rows.reduce<Record<string, GroupRow[]>>((acc, row) => {
    const key = String(row.block_id || row.name);
    (acc[key] ||= []).push(row);
    return acc;
  }, {}));
  return <div className="factor-lab-group-grid">{groups.map(([key, values]) => {
    const ordered = [...values].sort((a, b) => Number(a.bucket) - Number(b.bucket));
    const scale = Math.max(.0001, ...ordered.map(row => Math.abs(Number(row.forward_return_20) || 0)));
    return <article key={key}><b>{ordered[0]?.name || key}</b>{ordered.map(row => <div key={row.bucket}><span>G{row.bucket}</span><i><em className={Number(row.forward_return_20) >= 0 ? "up" : "down"} style={{ width: `${Math.max(2, Math.abs(Number(row.forward_return_20)) / scale * 100)}%` }}/></i><strong className={Number(row.forward_return_20) >= 0 ? "up" : "down"}>{pct(row.forward_return_20)}</strong></div>)}</article>;
  })}</div>;
}

function CorrelationMatrix({ rows, factors }: { rows: CorrelationRow[]; factors: SelectedFactorSummary[] }) {
  const names = factors.map(row => row.name);
  const valueFor = (a: string, b: string) => {
    if (a === b) return 1;
    const row = rows.find(item => (item.name_a === a && item.name_b === b) || (item.name_a === b && item.name_b === a));
    return Number(row?.correlation);
  };
  if (names.length < 2) return <div className="factor-lab-empty-chart">至少选择两个因子后显示相关性矩阵</div>;
  return <div className="factor-lab-corr" style={{ gridTemplateColumns: `150px repeat(${names.length}, minmax(72px,1fr))` }}>
    <span/>{names.map(name => <b key={`h-${name}`}>{name}</b>)}
    {names.flatMap(name => [<b key={`r-${name}`}>{name}</b>, ...names.map(other => { const value = valueFor(name, other); const opacity = Number.isFinite(value) ? .10 + Math.abs(value) * .55 : .04; return <span key={`${name}-${other}`} className={Math.abs(value) >= .7 && name !== other ? "warn" : ""} style={{ "--corr-opacity": opacity } as React.CSSProperties}>{Number.isFinite(value) ? value.toFixed(2) : "--"}</span>; })])}
  </div>;
}

export default function FactorLab({ onContextChange }: { onContextChange?: (context: ResearchContext) => void }) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [config, setConfig] = useState<LabConfig>({});
  const [selected, setSelected] = useState<FactorSelection[]>([]);
  const [customFactors, setCustomFactors] = useState<CustomFactor[]>([]);
  const [customDraft, setCustomDraft] = useState<CustomFactor>({ name: "", formula: "MOM(20)", direction: "higher", weight: .10 });
  const [result, setResult] = useState<FactorLabResult | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [tab, setTab] = useState("overview");
  const [history, setHistory] = useState<HistoryRow[]>(() => {
    if (typeof window === "undefined") return [];
    try { return JSON.parse(localStorage.getItem("quantweave-factor-lab-history") || "[]"); }
    catch { return []; }
  });

  useEffect(() => {
    fetch(`${API}/api/v1/factor-lab/catalog`, { cache: "no-store" }).then(async response => {
      const payload = await response.json() as Catalog & { detail?: string };
      if (!response.ok) throw new Error(payload.detail || "因子实验室服务不可用");
      setCatalog(payload);
      setConfig({ ...payload.defaults, name: "因子实验室研究", run_model: true, min_amount: 30_000_000, min_train_dates: 80, max_train_rows: 150_000 });
      const defaultFactors = payload.defaults.factors || [];
      setSelected(defaultFactors.map(id => ({ id, weight: 1 / Math.max(defaultFactors.length, 1) })));
    }).catch(reason => setError(reason instanceof Error ? reason.message : "因子实验室服务不可用"));
  }, []);

  const selectedDefinitions = useMemo(() => selected.map(item => ({ ...catalog?.factors.find(factor => factor.id === item.id), ...item })).filter(item => item.name), [catalog, selected]);
  const families = useMemo(() => [...new Set((catalog?.factors || []).map(item => item.family))], [catalog]);
  const modelMeta = catalog?.models.find(item => item.id === config.model);
  const baseline = result?.baseline;
  const model = result?.model_result;
  const modelSummary = result?.model_summary;

  useEffect(() => {
    const selectedNames = selectedDefinitions.map(item => item.name).filter(Boolean).join("、") || "尚未选择因子";
    onContextChange?.({
      surface: "factors",
      title: `因子实验室 · ${tab === "overview" ? "实验总览" : tab}`,
      summary: running ? "正在运行全市场因子诊断和样本外机器学习回测。" : result ? "正在复核因子诊断、模型表现和组合成交。" : "正在配置因子、权重、股票池和机器学习参数。",
      details: [`因子：${selectedNames}`, `股票池：${String(config.universe || "沪深京全A")}`, `状态：${running ? "计算中" : result ? "已有结果" : "待运行"}`],
      suggestions: ["解释当前因子实验最应该先看哪三个指标", "帮我判断这些因子是否有重复暴露", "把当前因子实验结果转成策略画布节点"],
    });
  }, [config.universe, onContextChange, result, running, selectedDefinitions, tab]);

  const toggleFactor = (id: string) => setSelected(current => current.some(item => item.id === id)
    ? current.filter(item => item.id !== id)
    : [...current, { id, weight: .10 }]);
  const updateWeight = (id: string, weight: number) => setSelected(current => current.map(item => item.id === id ? { ...item, weight } : item));
  const saveHistory = (next: HistoryRow[]) => { setHistory(next); localStorage.setItem("quantweave-factor-lab-history", JSON.stringify(next)); };

  const run = async () => {
    setRunning(true); setError("");
    try {
      const response = await fetch(`${API}/api/v1/factor-lab/run`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config: { ...config, factors: selected, custom_factors: customFactors } }),
      });
      const payload = await response.json() as FactorLabResult & { detail?: string };
      if (!response.ok) throw new Error(payload.detail || "因子实验运行失败");
      setResult(payload); setTab("overview");
      const row: HistoryRow = { id: crypto.randomUUID(), createdAt: new Date().toISOString(), name: config.name || "因子实验", model: modelMeta?.name || config.model || "未选择", annual: payload.model_result?.metrics?.annual_return ?? payload.model_result?.metrics?.cagr, rankIc: payload.model_summary?.rank_ic };
      saveHistory([row, ...history].slice(0, 20));
    } catch (reason: unknown) { setError(reason instanceof Error ? reason.message : "因子实验运行失败"); }
    finally { setRunning(false); }
  };

  const exportResult = () => {
    if (!result) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = `quantweave-factor-lab-${Date.now()}.json`; link.click(); URL.revokeObjectURL(url);
  };

  return <section className="factor-lab-shell">
    <header className="factor-lab-header"><div><span>FACTOR RESEARCH TERMINAL</span><h1>因子实验室</h1><p>单因子诊断 · 多因子组合 · Walk-forward机器学习 · A股次日开盘回测</p></div><div className="factor-lab-header-actions"><button onClick={exportResult} disabled={!result}>导出结果</button><button className="primary" onClick={run} disabled={running || !catalog || !selected.length}>{running ? "全市场计算中…" : "运行实验"}</button></div></header>
    <div className="factor-lab-layout">
      <aside className="factor-lab-config">
        <div className="factor-lab-section-title"><b>实验设置</b><span>所有信号按收盘冻结</span></div>
        <label>实验名称<input value={config.name || ""} onChange={event => setConfig(value => ({ ...value, name: event.target.value }))}/></label>
        <div className="factor-lab-two"><label>开始日期<input type="date" value={config.start_date || ""} onChange={event => setConfig(value => ({ ...value, start_date: event.target.value }))}/></label><label>结束日期<input type="date" value={config.end_date || ""} onChange={event => setConfig(value => ({ ...value, end_date: event.target.value }))}/></label></div>
        <div className="factor-lab-two"><label>股票池<select value={config.universe || "all"} onChange={event => setConfig(value => ({ ...value, universe: event.target.value }))}><option value="all">沪深京全A</option><option value="sh_sz">沪深A股</option><option value="main">沪深主板</option><option value="star">科创板</option><option value="chinext">创业板</option><option value="bj">北交所</option></select></label><label>成交额下限<input type="number" value={config.min_amount || 0} onChange={event => setConfig(value => ({ ...value, min_amount: Number(event.target.value) }))}/></label></div>
        <div className="factor-lab-section-title"><b>因子库</b><span>{selected.length + customFactors.length}/12</span></div>
        {families.map(family => <div className="factor-lab-family" key={family}><b>{family}</b>{catalog?.factors.filter(item => item.family === family).map(item => <label key={item.id} className={selected.some(value => value.id === item.id) ? "selected" : ""}><input type="checkbox" checked={selected.some(value => value.id === item.id)} onChange={() => toggleFactor(item.id)}/><span><strong>{item.name}</strong><small>{item.description}</small></span></label>)}</div>)}
        {selectedDefinitions.length > 0 && <div className="factor-lab-weights"><b>组合权重</b>{selectedDefinitions.map(item => <label key={item.id}><span>{item.name}</span><input type="number" min="0.01" max="1" step="0.01" value={item.weight} onChange={event => updateWeight(item.id, Number(event.target.value))}/></label>)}</div>}

        <div className="factor-lab-section-title"><b>自定义因子</b><span>安全公式</span></div>
        <label>名称<input value={customDraft.name} placeholder="例如：20日突破强度" onChange={event => setCustomDraft(value => ({ ...value, name: event.target.value }))}/></label>
        <label>公式<textarea value={customDraft.formula} onChange={event => setCustomDraft(value => ({ ...value, formula: event.target.value }))}/><small>支持 MOM、MA、RSI、VOL、ADV、DRAWDOWN</small></label>
        <div className="factor-lab-two"><label>方向<select value={customDraft.direction} onChange={event => setCustomDraft(value => ({ ...value, direction: event.target.value as "higher" | "lower" }))}><option value="higher">越大越好</option><option value="lower">越小越好</option></select></label><label>权重<input type="number" step="0.01" value={customDraft.weight} onChange={event => setCustomDraft(value => ({ ...value, weight: Number(event.target.value) }))}/></label></div>
        <button className="factor-lab-add" onClick={() => { if (customDraft.formula.trim()) { setCustomFactors(value => [...value, { ...customDraft, name: customDraft.name || `自定义因子${value.length + 1}` }]); setCustomDraft({ name: "", formula: "MOM(20)", direction: "higher", weight: .10 }); } }}>＋ 加入实验</button>
        {customFactors.map((item, index) => <div className="factor-lab-custom-row" key={`${item.name}-${index}`}><span><b>{item.name}</b><small>{item.formula}</small></span><button onClick={() => setCustomFactors(value => value.filter((_, current) => current !== index))}>×</button></div>)}
      </aside>

      <main className="factor-lab-main">
        <nav className="factor-lab-tabs">{[["overview", "实验总览"], ["factors", "单因子体检"], ["groups", "分组收益"], ["correlation", "相关性"], ["machine", "机器学习"], ["portfolio", "组合成交"]].map(([id, label]) => <button className={tab === id ? "active" : ""} onClick={() => setTab(id)} key={id}>{label}</button>)}</nav>
        {error && <div className="factor-lab-error"><b>实验未完成</b><span>{error}</span></div>}
        {!result && !error && <div className="factor-lab-intro"><div><b>从左侧选择因子并运行</b><span>系统会在同一个时间口径下完成因子诊断、模型滚动训练和A股真实约束回测。</span></div><ol><li>检查覆盖率、Rank IC、ICIR和五分组单调性</li><li>识别高度相关、重复暴露或阶段失效的因子</li><li>比较规则组合、LightGBM/XGBoost与融合结果</li><li>保存模型训练次数、标签成熟规则和参数版本</li></ol></div>}
        {result && tab === "overview" && <div className="factor-lab-content"><div className="factor-lab-kpis"><article><span>多因子年化</span><b className={Number(baseline?.metrics?.annual_return ?? baseline?.metrics?.cagr) >= 0 ? "up" : "down"}>{pct(baseline?.metrics?.annual_return ?? baseline?.metrics?.cagr)}</b><small>最大回撤 {pct(baseline?.metrics?.max_drawdown)}</small></article><article><span>模型年化</span><b className={Number(model?.metrics?.annual_return ?? model?.metrics?.cagr) >= 0 ? "up" : "down"}>{model ? pct(model.metrics?.annual_return ?? model.metrics?.cagr) : "未运行"}</b><small>最大回撤 {model ? pct(model.metrics?.max_drawdown) : "--"}</small></article><article><span>模型Rank IC</span><b>{num(modelSummary?.rank_ic, 4)}</b><small>ICIR {num(modelSummary?.icir)} · 正向 {pct(modelSummary?.positive_rate)}</small></article></div><section><div className="factor-lab-panel-head"><b>样本外净值对比</b><span>相同股票池、成本与T+1开盘撮合</span></div><div className="factor-lab-legend"><span><i className="series-1"/>多因子基线</span><span><i className="series-2"/>机器学习</span></div><EquityChart baseline={baseline?.equity || []} model={model?.equity || []}/></section><div className="factor-lab-notes">{Object.entries(result.notes || {}).map(([key, value]) => <div key={key}><b>{key}</b><span>{String(value)}</span></div>)}</div></div>}
        {result && tab === "factors" && <div className="factor-lab-content"><section><div className="factor-lab-panel-head"><b>单因子有效性</b><span>未来5/20日收益 · 按日横截面</span></div><div className="factor-lab-table-wrap"><table><thead><tr><th>因子</th><th>权重</th><th>Rank IC 5日</th><th>Rank IC 20日</th><th>ICIR</th><th>正向比例</th><th>五组差</th><th>样本</th></tr></thead><tbody>{(baseline?.factor_analysis || []).map(row => <tr key={row.block_id}><td><b>{row.name}</b><small>{row.direction === "lower" ? "越小越好" : "越大越好"}</small></td><td>{pct(row.weight)}</td><td>{num(row.rank_ic_5, 4)}</td><td>{num(row.rank_ic_20, 4)}</td><td>{num(row.icir_20)}</td><td>{pct(row.positive_rate_20)}</td><td className={Number(row.spread_20) >= 0 ? "up" : "down"}>{pct(row.spread_20)}</td><td>{Number(row.observations || 0).toLocaleString()}</td></tr>)}</tbody></table></div></section></div>}
        {result && tab === "groups" && <div className="factor-lab-content"><section><div className="factor-lab-panel-head"><b>五分组未来收益</b><span>G1低分 → G5因子偏好</span></div><GroupBars rows={baseline?.factor_groups || []}/></section></div>}
        {result && tab === "correlation" && <div className="factor-lab-content"><section><div className="factor-lab-panel-head"><b>因子相关性矩阵</b><span>|相关系数|≥0.70 标记为重复暴露风险</span></div><CorrelationMatrix rows={baseline?.factor_correlations || []} factors={result.selected_factors || []}/></section></div>}
        {result && tab === "machine" && <div className="factor-lab-content"><div className="factor-lab-model-summary"><div><span>模型</span><b>{modelSummary?.name || "未运行"}</b></div><div><span>滚动拟合次数</span><b>{modelSummary?.fits || 0}</b></div><div><span>预测样本</span><b>{Number(modelSummary?.predictions || 0).toLocaleString()}</b></div><div><span>样本外日期</span><b>{modelSummary?.dates || 0}</b></div></div><section><div className="factor-lab-panel-head"><b>每日Rank IC</b><span>只统计标签已经成熟的样本外预测</span></div><IcChart rows={modelSummary?.daily_ic || []}/></section><section><div className="factor-lab-panel-head"><b>特征重要性</b><span>仅用于解释，不代表因果关系</span></div><div className="factor-lab-importance">{(modelSummary?.feature_importance || []).map(row => <div key={row.feature}><span>{result.selected_factors?.find(item => `${item.block_id}__score` === row.feature)?.name || row.feature}</span><i><em style={{ width: `${Math.max(1, Number(row.importance) * 100)}%` }}/></i><b>{pct(row.importance)}</b></div>)}</div></section></div>}
        {result && tab === "portfolio" && <div className="factor-lab-content"><section><div className="factor-lab-panel-head"><b>模型成交明细</b><span>涨跌停、停牌、整手、费用、容量和T+1均参与撮合</span></div><div className="factor-lab-table-wrap"><table><thead><tr><th>日期</th><th>股票</th><th>方向</th><th>价格</th><th>数量</th><th>金额</th><th>原因</th></tr></thead><tbody>{(model?.trades || []).slice(-200).reverse().map((row, index) => <tr key={`${row.date}-${row.code}-${index}`}><td>{String(row.date).slice(0, 10)}</td><td><b>{row.name}</b><small>{row.code}</small></td><td className={row.side === "买入" ? "up" : "down"}>{row.side}</td><td>{num(row.price, 2)}</td><td>{Number(row.shares || 0).toLocaleString()}</td><td>{Number(row.amount || row.value || 0).toLocaleString()}</td><td>{row.reason || "模型调仓"}</td></tr>)}</tbody></table></div></section></div>}
      </main>

      <aside className="factor-lab-model">
        <div className="factor-lab-section-title"><b>机器学习</b><span>Walk-forward</span></div>
        <label className="factor-lab-switch"><input type="checkbox" checked={Boolean(config.run_model)} onChange={event => setConfig(value => ({ ...value, run_model: event.target.checked }))}/><span>运行机器学习对照</span></label>
        <label>模型<select value={config.model || "hist_gb"} onChange={event => setConfig(value => ({ ...value, model: event.target.value }))}>{catalog?.models.map(item => <option value={item.id} disabled={!item.available} key={item.id}>{item.name}{item.available ? "" : " · 未安装"}</option>)}</select>{modelMeta && !modelMeta.available && <small className="warn">{modelMeta.disabled_reason}</small>}</label>
        <label>收益标签<select value={config.label_horizon || 5} onChange={event => setConfig(value => ({ ...value, label_horizon: Number(event.target.value) }))}><option value="1">未来1日开盘收益</option><option value="5">未来5日开盘收益</option><option value="10">未来10日开盘收益</option><option value="20">未来20日开盘收益</option></select></label>
        <div className="factor-lab-two"><label>训练窗口<input type="number" min="60" max="1000" value={config.train_window || 252} onChange={event => setConfig(value => ({ ...value, train_window: Number(event.target.value) }))}/><small>交易日</small></label><label>重训间隔<input type="number" min="1" max="120" value={config.retrain_every || 20} onChange={event => setConfig(value => ({ ...value, retrain_every: Number(event.target.value) }))}/><small>交易日</small></label></div>
        <div className="factor-lab-two"><label>研究池<input type="number" min="20" max="1000" value={config.research_pool || 300} onChange={event => setConfig(value => ({ ...value, research_pool: Number(event.target.value) }))}/></label><label>最少训练日<input type="number" min="20" value={config.min_train_dates || 80} onChange={event => setConfig(value => ({ ...value, min_train_dates: Number(event.target.value) }))}/></label></div>

        <div className="factor-lab-section-title"><b>组合参数</b><span>A股约束</span></div>
        <div className="factor-lab-two"><label>持仓数<input type="number" min="1" max="30" value={config.positions || 8} onChange={event => setConfig(value => ({ ...value, positions: Number(event.target.value) }))}/></label><label>调仓间隔<input type="number" min="1" max="60" value={config.rebalance_every || 10} onChange={event => setConfig(value => ({ ...value, rebalance_every: Number(event.target.value) }))}/></label></div>
        <div className="factor-lab-safety">{Object.entries(catalog?.safety || {}).map(([key, value]) => <div key={key}><i/><span>{value}</span></div>)}</div>

        <div className="factor-lab-section-title"><b>最近实验</b><button onClick={() => saveHistory([])} disabled={!history.length}>清空</button></div>
        <div className="factor-lab-history">{history.map(row => <div key={row.id}><span><b>{row.name}</b><small>{new Date(row.createdAt).toLocaleString("zh-CN")} · {row.model}</small></span><strong className={Number(row.annual) >= 0 ? "up" : "down"}>{pct(row.annual)}</strong><em>IC {num(row.rankIc, 3)}</em></div>)}{!history.length && <p>运行结果会保留实验摘要</p>}</div>
      </aside>
    </div>
  </section>;
}
