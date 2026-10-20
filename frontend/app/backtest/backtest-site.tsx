"use client";

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  ArrowRight,
  BookOpen,
  Bot,
  Boxes,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  Clock3,
  Code2,
  Database,
  FileClock,
  FlaskConical,
  Gauge,
  HardDrive,
  History,
  Home,
  Info,
  LineChart,
  Menu,
  Play,
  ShieldCheck,
  Table2,
  X,
  type LucideIcon,
} from "lucide-react";
import QuantWorkflowStudio from "../quant-workflow";
import FactorLab from "../factor-lab";
import AiWorkspace from "../ai-workspace";
import type { ResearchContext } from "../research-context";

const API = "";

type View = "overview" | "studio" | "factors" | "ai" | "runs" | "data" | "guide";
type ServiceState = { status: "checking" | "online" | "offline"; latency?: number; detail?: string };
type CatalogPayload = {
  catalog?: Record<string, { category?: string; available?: boolean }>;
  saved?: Array<{ id?: string; name?: string }>;
  presets?: Record<string, unknown>;
};
type RunSummary = {
  run_id: string;
  created_at?: string;
  pipeline_name?: string;
  start?: string;
  end?: string;
  metrics?: Record<string, number>;
  coverage?: Record<string, number>;
};
type RunResult = RunSummary & {
  equity?: Array<Record<string, unknown>>;
  trades?: Array<Record<string, unknown>>;
  closed_trades?: Array<Record<string, unknown>>;
  stock_pnl?: Array<Record<string, unknown>>;
  rank_details?: Array<Record<string, unknown>>;
};
type MarketStatus = {
  database?: string;
  source?: string;
  bars?: number;
  instruments?: number;
  min_date?: string;
  max_date?: string;
  exported_at?: string;
  assets?: Record<string, { instruments?: number; bars?: number; min_date?: string; max_date?: string }>;
  coverage?: Record<string, { searchable?: number; with_bars?: number; missing?: number }>;
  periods?: Record<string, number>;
};
type SchedulerStatus = {
  enabled?: boolean;
  active?: boolean;
  status?: "waiting" | "running" | "ready" | "error";
  schedule?: string;
  daemon_started_at?: string | null;
  last_seen_at?: string | null;
  last_started_at?: string | null;
  last_finished_at?: string | null;
  last_success_date?: string | null;
  last_error?: string | null;
  next_retry_at?: string | null;
  next_scheduled_at?: string | null;
};
const navItems: Array<{ id: View; label: string; icon: LucideIcon; hint: string }> = [
  { id: "overview", label: "研究首页", icon: Home, hint: "从空白想法开始，查看数据和最近一次实验" },
  { id: "studio", label: "策略画布", icon: Boxes, hint: "像搭积木一样连接股票池、筛选、因子、组合和执行" },
  { id: "factors", label: "因子实验", icon: FlaskConical, hint: "检查 Rank IC、分组收益、相关性和滚动机器学习" },
  { id: "ai", label: "AI研究助手", icon: Bot, hint: "结合本地行情、工作流、回测与外部数据进行问答" },
  { id: "runs", label: "结果审计", icon: History, hint: "回看净值、回撤、每日排名和逐笔成交" },
  { id: "data", label: "数据中心", icon: Database, hint: "检查行情覆盖、更新时间和数据可用范围" },
  { id: "guide", label: "学习中心", icon: BookOpen, hint: "跟着实例把看盘想法翻译成可回测规则" },
];

const resolveView = (value?: string | null): View =>
  navItems.some(item => item.id === value) ? (value as View) : "overview";

const contextForView = (view: View): ResearchContext => {
  if (view === "studio") return { surface: "studio", title: "策略画布", summary: "正在把股票池、筛选、因子、组合和执行节点连接成可回测流程。", suggestions: ["告诉我这条工作流现在缺哪一个节点", "检查当前工作流是否有未来函数风险", "把当前流程改成五仓位、次日执行的规则"] };
  if (view === "factors") return { surface: "factors", title: "因子实验室", summary: "正在检查单因子有效性、多因子组合与机器学习样本外表现。", suggestions: ["解释当前因子实验最应该先看哪三个指标", "帮我判断这些因子是否有重复暴露", "把当前因子实验结果转成策略画布节点"] };
  if (view === "guide") return { surface: "guide", title: "学习中心", summary: "正在通过练习把看盘判断翻译成可验证的量化规则。", suggestions: ["用最简单的话解释我当前这一节", "把当前课程内容变成一条工作流", "给我一个类似当前知识点的练习"] };
  if (view === "runs") return { surface: "runs", title: "结果审计", summary: "正在复核净值曲线、回撤、每日排名和逐笔成交。" };
  if (view === "data") return { surface: "data", title: "数据中心", summary: "正在检查行情覆盖、更新时间和因子数据口径。" };
  if (view === "ai") return { surface: "ai", title: "AI 研究助手", summary: "正在结合当前会话、本地行情和回测结果回答研究问题。" };
  return { surface: "overview", title: "研究首页", summary: "正在从研究首页选择数据、工作流和回测结果。" };
};

const number = (value: unknown, digits = 0) => Number.isFinite(Number(value)) ? Number(value).toLocaleString("zh-CN", { maximumFractionDigits: digits, minimumFractionDigits: digits }) : "--";
const pct = (value: unknown, digits = 2) => Number.isFinite(Number(value)) ? `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(digits)}%` : "--";
const iso = (value?: string) => value ? value.replace("T", " ").replace(/\+08:00$/, "") : "--";
const localTime = (value?: string | null) => {
  if (!value) return "--";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? iso(value) : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
};

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json() as Promise<T>;
}

function EquityCurve({ rows, mode = "return" }: { rows: Array<Record<string, unknown>> | Array<{ timestamp: string; strategyReturn: number }>; mode?: "return" | "equity" }) {
  const points = useMemo(() => {
    if (!rows.length) return [];
    return rows.map((row, index) => {
      const record = row as Record<string, unknown>;
      const raw = mode === "return"
        ? Number((row as { strategyReturn?: number }).strategyReturn)
        : Number(record.equity ?? record.total_value ?? record.value ?? record.nav ?? record.close);
      const date = String(record.date ?? record.timestamp ?? index).slice(0, 10);
      return { value: raw, date };
    }).filter(item => Number.isFinite(item.value));
  }, [rows, mode]);
  if (points.length < 2) return <div className="bt-empty-chart"><LineChart />暂无可绘制的净值曲线</div>;
  const sampled = points.filter((_, index) => index % Math.max(1, Math.floor(points.length / 700)) === 0 || index === points.length - 1);
  const values = sampled.map(item => item.value);
  const min = Math.min(...values), max = Math.max(...values), range = Math.max(max - min, Math.abs(max) * .01, 1);
  const x = (index: number) => 56 + index / Math.max(sampled.length - 1, 1) * 820;
  const y = (value: number) => 18 + (max - value) / range * 194;
  const path = sampled.map((item, index) => `${index ? "L" : "M"}${x(index).toFixed(2)},${y(item.value).toFixed(2)}`).join(" ");
  const area = `${path} L876,222 L56,222 Z`;
  const ticks = [max, max - range / 2, min];
  return <div className="bt-equity-chart">
    <svg viewBox="0 0 900 250" role="img" aria-label={`策略曲线，从 ${sampled[0].date} 到 ${sampled.at(-1)?.date}`}>
      <defs><linearGradient id="bt-equity-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="currentColor" stopOpacity=".22"/><stop offset="1" stopColor="currentColor" stopOpacity="0"/></linearGradient></defs>
      {ticks.map((tick, index) => <g key={tick}><line x1="56" x2="876" y1={18 + index * 97} y2={18 + index * 97}/><text x="49" y={22 + index * 97} textAnchor="end">{mode === "return" ? `${tick.toFixed(1)}%` : number(tick, 0)}</text></g>)}
      <path className="area" d={area}/><path className="line" d={path}/>
      <text x="56" y="244">{sampled[0].date}</text><text x="876" y="244" textAnchor="end">{sampled.at(-1)?.date}</text>
    </svg>
  </div>;
}

function StatusDot({ state }: { state: ServiceState["status"] }) {
  return <i className={`bt-status-dot ${state}`} aria-hidden="true" />;
}

function PageHeader({ eyebrow, title, description, action }: { eyebrow: string; title: string; description: string; action?: ReactNode }) {
  return <header className="bt-page-head"><div><span>{eyebrow}</span><h1>{title}</h1><p>{description}</p></div>{action}</header>;
}

function ReactBitsParticleField() {
  const dataGlyphs = ["ALPHA", "IC", "SHARPE", "MAXDD", "RANK", "VOL", "BETA", "WIN%"];
  return <div className="bt-reactbits-bg" aria-hidden="true">
    {Array.from({ length: 32 }, (_, index) => <i key={index} />)}
    {dataGlyphs.map(item => <span key={item}>{item}</span>)}
  </div>;
}

function Overview({ catalog, runs, market, service, open }: { catalog: CatalogPayload | null; runs: RunSummary[]; market: MarketStatus | null; service: ServiceState; open: (view: View) => void }) {
  const nodes = Object.values(catalog?.catalog || {});
  const available = nodes.filter(item => item.available !== false).length;
  const last = runs[0];
  return <main className="bt-content bt-overview">
    <ReactBitsParticleField />
    <section className="bt-hero">
      <div className="bt-hero-energy" aria-hidden="true"><i/><i/><i/><i/></div>
      <div className="bt-hero-copy">
        <span>QUANTWEAVE RESEARCH OS</span>
        <h1>把看盘判断，拼成<br/><em>可验证的量化流程</em></h1>
        <p>从股票池、硬筛、因子、组合到执行，用节点把想法连接成可回测的研究流程；每一次运行都能回看数据范围、净值、成交和风险。</p>
        <div className="bt-hero-actions">
          <button className="bt-hero-primary" onClick={() => open("studio")}><Play />打开策略画布</button>
          <button onClick={() => open("guide")}><BookOpen />从案例开始</button>
        </div>
        <div className="bt-hero-pills" aria-label="研究能力">
          <span><StatusDot state={service.status}/>{service.status === "online" ? "回测引擎在线" : service.status === "checking" ? "正在检查引擎" : "引擎待连接"}</span>
          <span>{available || "--"} 个可用节点</span>
          <span>{market?.max_date || "本地行情"} 截止</span>
        </div>
        <div className="bt-home-data-stream" aria-hidden="true"><span>OHLCV</span><span>FACTOR RANK</span><span>BACKTEST</span><span>RISK CHECK</span><span>AUDIT TRAIL</span></div>
      </div>
      <div className="bt-hero-visual bt-research-cockpit" aria-label="量构研究驾驶舱">
        <div className="bt-cockpit-orbits" aria-hidden="true"><i/><i/><i/></div>
        <div className="bt-cockpit-pulses" aria-hidden="true"><b/><b/><b/><b/></div>
        <header><span>LIVE RESEARCH FLOW</span><b>研究流程准备中</b><small>{runs.length} 次运行记录</small></header>
        <div className="bt-cockpit-chart" aria-hidden="true">
          <i/><i/><i/><i/><i/><i/>
          <svg viewBox="0 0 420 120" role="img" aria-label="研究净值预览">
            <path d="M8 88 C52 78 62 46 105 58 S168 96 210 66 282 20 330 42 373 76 412 28"/>
          </svg>
        </div>
        <div className="bt-cockpit-flow">
          <article><Code2/><span>描述</span><small>规则转公式</small></article>
          <article><Boxes/><span>搭建</span><small>{available || "--"} 个模块</small></article>
          <article><ShieldCheck/><span>审计</span><small>成交可追溯</small></article>
        </div>
      </div>
    </section>
    <section className="bt-kpis" aria-label="平台概况">
      <article><Database/><span>日线记录</span><b>{market?.bars ? number(market.bars) : "2,176万+"}</b><small>{market?.min_date || "1990-12-19"} 至 {market?.max_date || "--"}</small></article>
      <article><Boxes/><span>节点能力</span><b>{nodes.length || "--"}</b><small>{available || "--"} 可运行 · {nodes.length ? nodes.length - available : "--"} 待补数据</small></article>
      <article><History/><span>保存的工作流</span><b>{catalog?.saved?.length ?? "--"}</b><small>工作流运行 {runs.length} 条</small></article>
      <article><Gauge/><span>回测服务</span><b className={service.status === "online" ? "healthy" : "warning"}>{service.status === "online" ? "在线" : service.status === "checking" ? "检查中" : "离线"}</b><small>{service.latency ? `${service.latency} ms` : "本机 8767 端口"}</small></article>
    </section>
    <div className="bt-overview-grid bt-overview-grid-solo">
      <aside className="bt-panel bt-readiness"><header><div><span>研究就绪度</span><h2>数据与执行检查</h2></div><ShieldCheck/></header>
        <ul>
          <li><CheckCircle2/><span><b>本地行情数据库</b><small>{market?.assets?.stock?.instruments ? `${number(market.assets.stock.instruments)} 只股票，截止 ${market.max_date}` : "由回测服务直接读取 DuckDB"}</small></span></li>
          <li><CheckCircle2/><span><b>真实因子注册表</b><small>{nodes.filter(item => item.category === "factor" && item.available !== false).length} 个因子可直接计算，无灰色摆设</small></span></li>
          <li><CheckCircle2/><span><b>A股交易约束</b><small>次日执行、涨跌停、整手、佣金与滑点</small></span></li>
          <li><Info/><span><b>研究边界</b><small>平台不连接实盘下单，历史结果不代表未来收益</small></span></li>
        </ul>
      </aside>
    </div>
    <section className="bt-module-grid"><button onClick={() => open("studio")}><Boxes/><span><b>自由搭建工作流</b><small>默认空白，节点可编辑、连接、保存和回访版本</small></span><ChevronRight/></button><button onClick={() => open("factors")}><FlaskConical/><span><b>验证因子有效性</b><small>比较多因子基线与 Walk-forward 机器学习</small></span><ChevronRight/></button><button onClick={() => open("data")}><HardDrive/><span><b>检查数据覆盖</b><small>确认股票、ETF、指数与日线行情更新时间</small></span><ChevronRight/></button></section>
    {last && <p className="bt-data-caption">最近运行：{iso(last.created_at)} · 所有结果只用于研究，不会自动变成交易建议。</p>}
  </main>;
}

function RunRecords({ runs }: { runs: RunSummary[] }) {
  const [selected, setSelected] = useState<RunResult | null>(null);
  const [loading, setLoading] = useState("");
  const [error, setError] = useState("");
  const loadRun = async (id: string) => { setLoading(id); setError(""); try { setSelected(await getJson<RunResult>(`${API}/api/v1/lego/runs/${id}`)); } catch (reason) { setError(reason instanceof Error ? reason.message : "读取失败"); } finally { setLoading(""); } };
  return <main className="bt-content">
    <PageHeader eyebrow="RESULT AUDIT" title="结果审计" description="每次回测保存参数、数据范围、净值、每日排名和逐笔成交；点击记录可复核结果。"/>
    <section className="bt-panel bt-runs-panel"><header><div><span>工作流运行</span><h2>量构策略回测记录</h2></div><b>{runs.length} 条</b></header>
      {runs.length ? <div className="bt-table-wrap"><table><thead><tr><th>运行时间</th><th>策略</th><th>区间</th><th>累计收益</th><th>最大回撤</th><th>操作</th></tr></thead><tbody>{runs.map(run => <tr key={run.run_id}><td>{iso(run.created_at)}</td><td><b>{run.pipeline_name || "未命名工作流"}</b><small>{run.run_id}</small></td><td>{run.start || "--"} → {run.end || "--"}</td><td className={Number(run.metrics?.total_return || 0) >= 0 ? "up" : "down"}>{pct(Number(run.metrics?.total_return || 0) * 100)}</td><td className="down">{pct(Number(run.metrics?.max_drawdown || 0) * 100)}</td><td><button onClick={() => loadRun(run.run_id)} disabled={loading === run.run_id}>{loading === run.run_id ? "读取中" : "打开"}</button></td></tr>)}</tbody></table></div> : <div className="bt-empty compact"><FileClock/><b>还没有工作流回测记录</b><span>进入策略工作站，连接完整流程并成功运行后会自动出现在这里。</span></div>}
      {error && <div className="bt-inline-error"><CircleAlert/>{error}</div>}
      {selected && <div className="bt-run-detail"><header><h3>{selected.pipeline_name || selected.run_id}</h3><button onClick={() => setSelected(null)}><X/>关闭详情</button></header><EquityCurve rows={selected.equity || []} mode="equity"/><div className="bt-result-strip"><div><span>累计收益</span><b>{pct(Number(selected.metrics?.total_return || 0) * 100)}</b></div><div><span>最大回撤</span><b>{pct(Number(selected.metrics?.max_drawdown || 0) * 100)}</b></div><div><span>成交记录</span><b>{number(selected.trades?.length)}</b></div><div><span>股票盈亏</span><b>{number(selected.stock_pnl?.length)}</b></div></div></div>}
    </section>
  </main>;
}

function DataCenter({ market, scheduler }: { market: MarketStatus | null; scheduler: SchedulerStatus | null }) {
  const assets = market?.assets || {};
  const schedulerState = scheduler?.status || "waiting";
  const schedulerStarted = Boolean(scheduler?.active);
  const schedulerLabel = !scheduler ? "检查中" : !scheduler.enabled ? "已关闭" : !schedulerStarted ? "待启动" : schedulerState === "running" ? "更新中" : schedulerState === "error" ? "需要重试" : "已启用";
  const schedulerDetail = !scheduler ? "正在读取本机调度器状态" : !scheduler.enabled ? "设置 RUIPAN_DATA_SCHEDULER=1 后启用" : !schedulerStarted ? "重启 QuantWeave 后自动运行" : "随 QuantWeave 启动并保持运行";
  const nextRun = scheduler?.next_retry_at || scheduler?.next_scheduled_at;
  return <main className="bt-content"><PageHeader eyebrow="DATA CENTER" title="数据中心" description="所有回测结果都应能追溯到数据范围、更新时间和研究口径；缺失的数据不会被静默补成可用。"/>
    <section className="bt-kpis"><article><span>证券总数</span><b>{number(market?.instruments)}</b><small>股票、基金与指数</small></article><article><span>日线总量</span><b>{number(market?.bars)}</b><small>{market?.min_date || "--"} 起</small></article><article><span>最近交易日</span><b>{market?.max_date || "--"}</b><small>导出 {iso(market?.exported_at)}</small></article><article><span>因子数据源</span><b className="healthy">本地 OHLCV</b><small>含成交量与成交额</small></article></section>
    <section className={`bt-panel bt-scheduler-panel ${schedulerState}`}><header><div><span>DAILY MARKET UPDATE</span><h2>自动行情更新</h2></div><Clock3/></header>
      <div className="bt-scheduler-grid"><div><span>运行状态</span><b className={schedulerState === "error" || scheduler?.enabled === false || !schedulerStarted ? "warning" : scheduler ? "healthy" : ""}>{schedulerLabel}</b><small>{schedulerDetail}</small></div><div><span>执行计划</span><b>工作日 {scheduler?.schedule || "18:10"}</b><small>收盘后增量写入本地 DuckDB</small></div><div><span>上次完成</span><b>{localTime(scheduler?.last_finished_at)}</b><small>{scheduler?.last_success_date ? `最近成功交易日 ${scheduler.last_success_date}` : "尚未完成首次自动更新"}</small></div><div><span>{scheduler?.next_retry_at ? "下次重试" : "下一次计划"}</span><b>{localTime(nextRun)}</b><small>{scheduler?.next_retry_at ? "更新失败后每 30 分钟重试" : "只在工作日执行一次"}</small></div></div>
      <p className={`bt-panel-note ${scheduler?.last_error ? "bt-scheduler-error" : ""}`}>{scheduler?.last_error ? <><CircleAlert/>最近错误：{scheduler.last_error}</> : <><CheckCircle2/>调度器只更新本机日线数据库，不会上传本地行情、策略或密钥。</>}</p>
    </section>
    <div className="bt-data-grid"><section className="bt-panel"><header><div><span>MARKET DATABASE</span><h2>本地行情覆盖</h2></div><Database/></header><div className="bt-table-wrap"><table><thead><tr><th>资产</th><th>证券数</th><th>日线数</th><th>起始</th><th>截止</th><th>缺 K 线</th></tr></thead><tbody>{[["stock", "A股"], ["fund", "基金 / ETF"], ["index", "指数"]].map(([id, label]) => <tr key={id}><td><b>{label}</b><small>{id}</small></td><td>{number(assets[id]?.instruments)}</td><td>{number(assets[id]?.bars)}</td><td>{assets[id]?.min_date || "--"}</td><td>{assets[id]?.max_date || "--"}</td><td className={(market?.coverage?.[id]?.missing || 0) > 0 ? "warning" : "healthy"}>{number(market?.coverage?.[id]?.missing)}</td></tr>)}</tbody></table></div><p className="bt-panel-note"><Info/>股票池只会读取有历史行情的证券。缺失列表应先补数据，再参与全市场回测。</p></section>
      <section className="bt-panel"><header><div><span>FACTOR DATA CONTRACT</span><h2>因子计算口径</h2></div><Gauge/></header><dl className="bt-definition-list"><div><dt>来源</dt><dd>本地 DuckDB 日线</dd></div><div><dt>字段</dt><dd>开高低收、成交量、成交额</dd></div><div><dt>递推指标</dt><dd>EMA、MACD、KDJ、OBV</dd></div><div><dt>滚动指标</dt><dd>MA、RSI、ATR、CCI、MFI、CMF 等</dd></div><div><dt>时点安全</dt><dd>只读取信号日及此前完成行情</dd></div><div><dt>用途限制</dt><dd>仅研究 / 回测，不连接实盘</dd></div></dl><p className="bt-panel-note"><CheckCircle2/>节点库中的因子与后端注册表共用同一份定义，未实现的因子不会显示为可用。</p></section>
    </div>
    <section className="bt-panel bt-data-pipeline"><header><div><span>DATA CONTRACT</span><h2>回测数据口径</h2></div><ShieldCheck/></header><div><article><Clock3/><b>信号时点</b><span>日线策略默认收盘后冻结信号；如使用 14:45 选股，必须明确使用对应分钟线，不能读取收盘后的数据。</span></article><article><Table2/><b>复权与撮合</b><span>研究信号与成交价格采用一致的复权约定；成交使用次日可得价格并扣除费用、滑点和税费。</span></article><article><FileClock/><b>结果留痕</b><span>保存参数、数据截止日、运行时间、净值、每日排名和逐笔交易，才能复现结果。</span></article></div><small>当前数据源：{market?.source || "本地 DuckDB"} · 不上传个人策略和本地行情。</small></section>
  </main>;
}

type LearningExercise = { question: string; options: string[]; correct: number; explanation: string };

type LearningLesson = {
  id: string;
  title: string;
  summary: string;
  goal: string;
  concepts: Array<{ name: string; detail: string }>;
  example: { title: string; before: string; after: string; note: string };
  exercise: LearningExercise;
  moreExercises: LearningExercise[];
  action?: { label: string; view: View };
};

const learningLessons: LearningLesson[] = [
  {
    id: "rule",
    title: "把看盘判断写成规则",
    summary: "把“看起来不错”改成每只股票、每个交易日都能得到相同答案的条件。",
    goal: "学完后，你能把一条均线、动量或 RSI 判断翻译成公式。",
    concepts: [
      { name: "字段", detail: "K 线中可直接读取的数据，例如开盘价 open、最高价 high、最低价 low、收盘价 close 和成交量 volume。" },
      { name: "指标", detail: "用历史字段计算出的数值，例如 MA(20) 是最近 20 个交易日的平均收盘价。" },
      { name: "条件", detail: "结果只有“满足”或“不满足”的判断，例如 close > MA(20)。" },
    ],
    example: { title: "站上 20 日均线", before: "看盘语言：收盘价站上 20 日均线", after: "close > MA(20)", note: "先只判断“是否在均线上方”。如果要判断当天刚刚突破，还需要比较前一日状态。" },
    exercise: { question: "“14 日 RSI 低于 30”应该写成哪条规则？", options: ["RSI(14) < 30", "RSI(30) < 14", "close < 30"], correct: 0, explanation: "RSI(14) 表示 14 日 RSI，低于使用 <，所以答案是 RSI(14) < 30。" },
    moreExercises: [
      { question: "“收盘价在 20 日均线上方”和“今天刚刚突破 20 日均线”是否是同一条规则？", options: ["是，只要 close > MA(20)", "不是，刚刚突破还要比较昨天的状态", "是，只要今天上涨"], correct: 1, explanation: "close > MA(20) 只能说明现在在线上；要确认刚突破，还要验证昨天不在线上。" },
      { question: "MOM(20) > 0.05 表达的含义是什么？", options: ["20 日均线高于 5 元", "20 日动量大于 5%", "成交量连续 20 日上涨"], correct: 1, explanation: "MOM(20) 是 20 日价格动量，0.05 对应 5%。" },
    ],
  },
  {
    id: "timing",
    title: "分清信号和成交时点",
    summary: "你在什么时候知道信号，决定了回测最早能在什么时候成交。",
    goal: "学完后，你能避开最常见的未来数据错误。",
    concepts: [
      { name: "信号日 T", detail: "策略完成判断的交易日。使用 T 日收盘价，意味着要等 T 日收盘后才能确认信号。" },
      { name: "执行日 T+1", detail: "信号确认后的下一个交易日。日线策略通常从 T+1 的可成交价格开始撮合。" },
      { name: "未来数据", detail: "在当时还不知道的数据。例如 14:45 选股却使用当天最终收盘价，就偷看了未来 15 分钟。" },
    ],
    example: { title: "收盘后选股", before: "3 月 10 日收盘后确认 close > MA(20)", after: "3 月 11 日按执行模型尝试买入", note: "如果次日停牌或涨停买不到，回测必须如实记录未成交，而不是强行成交。" },
    exercise: { question: "策略使用当天完整日线收盘价生成信号，哪种成交设置更可信？", options: ["当天 14:45 买入", "当天收盘价买入", "下一交易日可成交价格买入"], correct: 2, explanation: "完整收盘价在收盘后才确定，因此最早应从下一交易日的可成交价格开始执行。" },
    moreExercises: [
      { question: "如果策略规定 14:45 选股，当天可以使用哪类数据？", options: ["当天最终收盘价", "截至 14:45 已经产生的分钟数据", "当天 15:00 后公布的数据"], correct: 1, explanation: "14:45 时只能看到这一时刻及以前的数据，最终收盘价当时还未知。" },
      { question: "T+1 买入日遇到一字涨停，回测应该怎么处理？", options: ["按涨停价强制成交", "按昨收价成交", "记录无法买入，并按既定规则等待或放弃"], correct: 2, explanation: "真实市场无法成交时，回测也不能虚构成交，否则收益会被高估。" },
    ],
  },
  {
    id: "workflow",
    title: "搭出第一条工作流",
    summary: "像搭积木一样按顺序连接范围、淘汰规则、排序、持仓和成交。",
    goal: "学完后，你能从空白画布搭出一条结构完整的策略。",
    concepts: [
      { name: "股票池", detail: "规定研究范围，例如沪深 A 股、创业板、ETF 或自定义代码池。" },
      { name: "硬筛", detail: "不合格就直接淘汰，例如 ST、停牌、上市不足 120 日或成交额太低。" },
      { name: "因子排序", detail: "给通过硬筛的股票打分。分数用于比较强弱，不负责决定股票能否交易。" },
      { name: "组合与执行", detail: "决定持有几只、每只多大仓位、何时调仓，以及费用和无法成交时如何处理。" },
    ],
    example: { title: "最小可运行链路", before: "沪深 A 股 → 排除 ST 与停牌 → 20 日动量排序", after: "取前 5 名 → 等权 → 每周调仓 → 次日执行", note: "每一段只承担一种职责。运行失败时，也能快速看出是哪一个节点的问题。" },
    exercise: { question: "下面哪一个顺序最符合完整的量化工作流？", options: ["因子 → 股票池 → 成交 → 硬筛", "股票池 → 硬筛 → 因子 → 组合 → 执行", "股票池 → 收益曲线 → 因子"], correct: 1, explanation: "先确定范围，再淘汰不可选股票，然后排序、构建持仓，最后模拟成交。" },
    moreExercises: [
      { question: "“排除 ST”和“按动量从高到低排序”分别属于什么？", options: ["都是硬筛", "硬筛与因子排序", "都是执行模型"], correct: 1, explanation: "ST 股票直接淘汰属于硬筛；动量数值用于比较候选股票，属于因子排序。" },
      { question: "策略取因子排名前 5 名并等权配置，每只股票的目标仓位约为多少？", options: ["5%", "20%", "50%"], correct: 1, explanation: "5 只等权时，每只目标仓位约为 1/5，也就是 20%，实际还要考虑整手和现金余量。" },
    ],
    action: { label: "去策略画布搭建", view: "studio" },
  },
  {
    id: "first-run",
    title: "完成第一次回测",
    summary: "先用简单策略跑通全流程，再增加条件；第一目标是可信，不是收益最高。",
    goal: "学完后，你能正确设置区间、持仓、调仓、费用和滑点。",
    concepts: [
      { name: "回测区间", detail: "建议先选一段包含上涨、下跌和震荡的时期，确认逻辑后再扩大到十年。" },
      { name: "仓位规则", detail: "例如最多持有 5 只、等权配置。仓位数量和补位逻辑必须在运行前固定。" },
      { name: "交易成本", detail: "佣金、印花税和滑点会直接降低收益，高换手策略尤其明显。" },
      { name: "基准", detail: "用于比较的市场表现，例如沪深 300。跑赢基准才说明策略可能提供了额外价值。" },
    ],
    example: { title: "第一条练习策略", before: "沪深 A 股中排除 ST、停牌和低成交额股票", after: "按 20 日动量取前 5 名，每周等权调仓，T+1 执行并计入费用", note: "先固定参数运行一次。不要看到结果后反复调整到最好，否则很容易只适合这段历史。" },
    exercise: { question: "第一次回测最合理的目标是什么？", options: ["把参数调到历史收益最高", "先确认数据、时点、成交和结果都能复现", "直接接模拟盘"], correct: 1, explanation: "先跑通并确认口径可信。收益优化和更复杂模型应放在流程可靠之后。" },
    moreExercises: [
      { question: "为什么回测必须设置佣金、印花税和滑点？", options: ["让曲线更好看", "模拟交易中真实存在的收益损耗", "它们只影响实盘，不影响回测"], correct: 1, explanation: "交易成本会减少真实可得收益，换手越高影响通常越大。" },
      { question: "策略收益 20%，同期沪深 300 收益 25%，首先应该如何理解？", options: ["策略一定成功", "策略赚钱但暂时跑输基准，需要继续分析", "基准与策略无关"], correct: 1, explanation: "绝对收益为正不代表提供了额外价值，还要与同区间基准比较。" },
    ],
    action: { label: "打开策略画布", view: "studio" },
  },
  {
    id: "results",
    title: "看懂回测结果",
    summary: "不要只看最终赚了多少；收益路径、最坏亏损和每笔交易同样重要。",
    goal: "学完后，你能用净值、回撤、基准和成交记录解释一次回测。",
    concepts: [
      { name: "净值曲线", detail: "从初始资金出发，账户价值随时间的变化。平滑不代表没有风险，要结合回撤看。" },
      { name: "最大回撤", detail: "历史最高点到随后最低点的最大跌幅。它回答“最难熬的时候会亏多少”。" },
      { name: "胜率", detail: "盈利交易占已完成交易的比例。胜率高不等于赚钱，还要比较平均盈利与平均亏损。" },
      { name: "换手率", detail: "持仓被替换的频率。换手越高，成本和实际成交难度通常越大。" },
    ],
    example: { title: "两个策略如何比较", before: "策略 A 收益 35%，最大回撤 -32%", after: "策略 B 收益 28%，最大回撤 -12%", note: "不能只凭总收益认定 A 更好。还要结合你的风险承受能力、基准、交易次数和收益稳定性。" },
    exercise: { question: "哪项指标最直接回答“历史上从高点最多跌了多少”？", options: ["胜率", "最大回撤", "成交次数"], correct: 1, explanation: "最大回撤衡量净值从阶段高点到后续低点的最大跌幅。" },
    moreExercises: [
      { question: "策略胜率 70%，是否足以证明策略赚钱？", options: ["足够，胜率超过一半就一定赚钱", "不够，还要比较平均盈利、平均亏损和交易成本", "足够，只要交易次数多"], correct: 1, explanation: "少数大亏可能抵消许多小赚，所以胜率必须和盈亏比、成本一起看。" },
      { question: "80% 的总收益只来自一只股票，说明什么？", options: ["策略非常稳定", "收益过度集中，需要检查偶然性和容量", "说明应该删除其他股票"], correct: 1, explanation: "收益集中意味着组合结果依赖极少数样本，策略的可重复性通常更弱。" },
    ],
    action: { label: "查看结果审计", view: "runs" },
  },
  {
    id: "trust",
    title: "判断结果是否可信",
    summary: "能跑出曲线不等于策略有效，还要确认它没有偷看未来，也不是只碰巧适合一段历史。",
    goal: "学完后，你能完成一次基础的回测可信度检查。",
    concepts: [
      { name: "样本外", detail: "一段完全不参与选规则和调参数的数据，只在策略定稿后用来检验。" },
      { name: "参数扰动", detail: "把 MA(20) 改成 MA(18) 或 MA(22) 后结果是否仍接近。只在单一参数点特别好，通常不稳健。" },
      { name: "收益集中", detail: "检查收益是否只来自少数股票或短暂行情。过度集中意味着结果更可能是偶然。" },
      { name: "市场阶段", detail: "分别观察上涨、下跌和震荡时期。策略可能只适用于其中一种环境。" },
    ],
    example: { title: "留出最后一年", before: "先用 2016—2024 年设计和调整策略", after: "规则锁定后，只在 2025 年做一次样本外检验", note: "如果看过 2025 年结果后再改策略，这一年就不再是样本外，需要重新留出未见数据。" },
    exercise: { question: "MA(20) 很好，但 MA(19) 和 MA(21) 都明显亏损，最合理的判断是？", options: ["20 是完美参数，可以直接使用", "结果可能对参数过度敏感，需要继续验证", "均线策略一定有效"], correct: 1, explanation: "只在一个参数点表现突出，往往是历史巧合或过度拟合的信号。" },
    moreExercises: [
      { question: "看过 2025 年测试结果后又修改了参数，2025 年还能叫样本外吗？", options: ["能，只要没有改股票池", "不能，因为结果已经参与了策略调整", "能，只要最终收益为正"], correct: 1, explanation: "任何看过并用于调整策略的数据，都已经进入研究过程，不再是真正未见的样本外数据。" },
      { question: "策略只在牛市赚钱，震荡和下跌阶段都明显失效，下一步最合理的是？", options: ["隐藏其他阶段结果", "明确适用市场环境并加入分阶段验证", "只回测牛市"], correct: 1, explanation: "策略可以有适用环境，但必须明确边界并验证环境识别是否使用当时可得数据。" },
    ],
    action: { label: "去因子实验验证", view: "factors" },
  },
];

const additionalLearningExercises: Record<string, LearningExercise[]> = {
  rule: [
    { question: "MA(5) 中的数字 5 表示什么？", options: ["价格是 5 元", "使用最近 5 个交易日计算", "每天交易 5 次"], correct: 1, explanation: "括号里的参数是计算窗口，MA(5) 使用最近 5 个交易日的数据。" },
    { question: "close < MA(20) 表示什么？", options: ["收盘价低于 20 日均线", "收盘价低于 20 元", "20 日成交量下降"], correct: 0, explanation: "close 是收盘价，MA(20) 是 20 日均线，< 表示低于。" },
    { question: "下面哪个字段代表成交量？", options: ["open", "volume", "high"], correct: 1, explanation: "volume 是成交量；open 是开盘价，high 是最高价。" },
    { question: "RSI 指标通常用来观察什么？", options: ["价格强弱和超买超卖", "上市公司员工数量", "股票代码长度"], correct: 0, explanation: "RSI 根据近期涨跌衡量价格强弱，常用于辅助观察超买超卖。" },
    { question: "ADV(20) > 30000000 中的 ADV 更接近哪类指标？", options: ["平均成交额", "平均股价", "平均上市年限"], correct: 0, explanation: "ADV 表示一段窗口内的平均成交额，适合用来限制流动性。" },
    { question: "为什么同一条公式在不同股票上可以得到不同结果？", options: ["每只股票的历史 K 线数据不同", "公式会随机改变", "股票代码会改变运算符"], correct: 0, explanation: "规则固定，但输入的价格和成交量历史不同，所以结果会不同。" },
    { question: "把“上涨很强”写成量化规则前，最先需要补充什么？", options: ["明确上涨的时间窗口和阈值", "直接买入", "删掉所有亏损样本"], correct: 0, explanation: "量化规则必须明确观察窗口和判断阈值，不能只使用模糊形容词。" },
  ],
  timing: [
    { question: "使用 T 日收盘价生成信号时，T 日收盘价何时才确定？", options: ["T 日开盘前", "T 日收盘后", "T-1 日收盘后"], correct: 1, explanation: "完整收盘价只有交易结束后才能确定。" },
    { question: "如果用 T 日收盘价下单，却按 T 日最低价成交，主要问题是什么？", options: ["使用了当时无法预知的价格", "手续费太高", "股票池太大"], correct: 0, explanation: "T 日最低价是收盘后才知道的，按它成交属于未来数据。" },
    { question: "14:45 选股的分钟线至少要包含到哪个时点？", options: ["前一交易日 15:00", "当天 14:45", "当天 15:30"], correct: 1, explanation: "策略只能使用做出决定时已经产生的数据，不能使用 14:45 之后的数据。" },
    { question: "A 股 T+1 规则对日线回测的直接影响是什么？", options: ["当天买入通常不能当天卖出", "可以无限次买卖", "只影响股票名称显示"], correct: 0, explanation: "T+1 下，当天买入的股票通常要到下一个交易日才能卖出。" },
    { question: "当天没有行情数据时，最可靠的处理方式是什么？", options: ["用前一天价格静默补齐", "标记缺失并排除或暂停该信号", "用未来一天价格填入"], correct: 1, explanation: "静默补齐会改变成交和收益，应明确标记缺失并按预设规则处理。" },
    { question: "为什么执行模型要提前固定？", options: ["避免看到结果后选择最有利的成交方式", "让界面颜色统一", "减少股票代码位数"], correct: 0, explanation: "先固定执行口径，才能避免回看结果后挑选有利价格。" },
    { question: "信号日和执行日分开后，净值曲线通常会怎样？", options: ["更接近真实可交易路径", "一定变成正收益", "不再需要交易成本"], correct: 0, explanation: "分开时点会把等待、无法成交和隔夜变化纳入结果，更接近真实交易。" },
  ],
  workflow: [
    { question: "股票池节点的主要职责是什么？", options: ["定义允许研究的证券范围", "计算最大回撤", "收取佣金"], correct: 0, explanation: "股票池先规定策略要从哪些证券中寻找机会。" },
    { question: "硬筛节点不通过时，后面的高分因子应该怎样处理？", options: ["仍然买入", "直接淘汰，不进入组合", "把分数翻倍"], correct: 1, explanation: "硬筛是交易资格门槛，因子高分不能绕过资格限制。" },
    { question: "为什么建议每个节点只负责一种职责？", options: ["更容易定位错误和复用模块", "可以隐藏回测结果", "能保证收益为正"], correct: 0, explanation: "职责清晰时，运行异常能快速定位，也更容易替换某个模块。" },
    { question: "想限制单只股票不能超过 20% 仓位，应该放在哪类节点？", options: ["组合与仓位", "股票池", "数据导入"], correct: 0, explanation: "持仓比例属于组合和仓位规则，不是股票范围或数据字段。" },
    { question: "工作流连线后运行失败，第一步应该看什么？", options: ["节点输入输出和错误提示", "直接删除所有节点", "只看最终收益"], correct: 0, explanation: "先从具体节点的输入、输出和校验提示定位问题，避免盲目修改。" },
    { question: "保存工作流的主要价值是什么？", options: ["记录规则，便于复现和比较版本", "自动保证未来赚钱", "替代行情数据"], correct: 0, explanation: "保存规则和参数可以复现回测，也能比较不同版本的变化。" },
    { question: "一个完整策略至少应连接哪些核心模块？", options: ["股票池、筛选/因子、组合、执行", "只有一个收益曲线", "只有股票名称和颜色"], correct: 0, explanation: "完整链路要覆盖研究范围、选股逻辑、仓位和成交。" },
  ],
  "first-run": [
    { question: "第一次回测为什么不建议一开始就上机器学习？", options: ["先要确认数据、时点和执行链路正确", "机器学习不能使用历史数据", "机器学习一定不能回测"], correct: 0, explanation: "基础口径未验证时，模型越复杂越难判断问题来自数据还是模型。" },
    { question: "回测区间最好包含哪些市场环境？", options: ["只包含上涨", "上涨、下跌和震荡等不同阶段", "只包含最近一个月"], correct: 1, explanation: "多种环境能帮助判断策略是否只适合单一行情。" },
    { question: "最多持有 5 只股票属于哪项设置？", options: ["持仓数量", "数据频率", "股票代码格式"], correct: 0, explanation: "它规定组合同时容纳多少只股票，属于组合设置。" },
    { question: "每周调仓一次意味着什么？", options: ["每天都必须交易", "按预设的每周时点重新评估并调整持仓", "只在周末使用未来数据"], correct: 1, explanation: "调仓频率规定何时重新生成组合，不代表每次一定要买卖。" },
    { question: "如果策略换手很高，哪项参数尤其需要检查？", options: ["交易成本和滑点", "股票名称字体", "页面背景颜色"], correct: 0, explanation: "高换手会放大佣金、税费和滑点对收益的影响。" },
    { question: "为什么第一次运行要保留基准指数？", options: ["比较策略是否提供超越市场的表现", "让策略自动买入指数", "基准可以替代股票池"], correct: 0, explanation: "基准提供同区间的市场参照，帮助理解策略的相对表现。" },
    { question: "运行前固定参数的好处是什么？", options: ["减少看到结果后反复试到最好", "保证没有亏损", "避免记录交易"], correct: 0, explanation: "先固定参数能降低过度拟合和事后挑选的风险。" },
  ],
  results: [
    { question: "累计收益和年化收益有什么区别？", options: ["累计收益看整个区间，年化收益换算为年度节奏", "两者永远相同", "年化收益只看单笔交易"], correct: 0, explanation: "年化收益把不同长度区间换算到可比较的年度尺度。" },
    { question: "最大回撤为 -25% 直观表示什么？", options: ["历史最高净值到随后低点最多跌约 25%", "每天必跌 25%", "每笔交易亏 25%"], correct: 0, explanation: "最大回撤描述净值从峰值到谷值的最大下降幅度。" },
    { question: "为什么要查看逐笔成交，而不是只看曲线？", options: ["发现单笔异常、无法成交和个股盈亏来源", "逐笔记录可以预测未来", "曲线不需要保存"], correct: 0, explanation: "成交明细能解释曲线由哪些买卖、成本和股票贡献。" },
    { question: "平均盈利小于平均亏损时，胜率需要怎样才可能覆盖损失？", options: ["通常需要更高胜率", "胜率越低越好", "与胜率完全无关"], correct: 0, explanation: "盈亏比不利时，需要更高比例的盈利交易才能抵消亏损。" },
    { question: "净值曲线创新高但换手率也很高，应该注意什么？", options: ["成本和实际成交容量可能被低估", "一定没有风险", "可以删除交易成本"], correct: 0, explanation: "高换手策略对成本和市场容量敏感，纸面收益可能难以复现。" },
    { question: "策略收益 30%，基准收益 35%，应如何描述？", options: ["绝对收益为正但相对基准跑输", "策略一定优于基准", "基准收益不应查看"], correct: 0, explanation: "同时报告绝对收益和相对基准表现，结论才完整。" },
    { question: "按股票查看盈亏的主要目的是什么？", options: ["判断收益是否过度依赖少数个股", "自动挑选未来股票", "隐藏组合回撤"], correct: 0, explanation: "个股分解能揭示收益来源、集中度和异常交易。" },
  ],
  trust: [
    { question: "未来函数最常见的表现是什么？", options: ["用信号产生后才知道的数据", "使用历史收盘价", "记录手续费"], correct: 0, explanation: "任何在决策时点不可获得的数据都会造成未来函数。" },
    { question: "什么是幸存者偏差？", options: ["只用今天仍存在的股票回看历史", "把手续费计入", "使用多个市场阶段"], correct: 0, explanation: "忽略退市、停牌或曾经存在但已消失的股票，会高估历史表现。" },
    { question: "样本外数据应该在什么时候使用？", options: ["规则和参数锁定之后", "每次调参都看一遍", "先看结果再设计规则"], correct: 0, explanation: "样本外用于最终检验，不能反复参与设计过程。" },
    { question: "参数扰动测试的目的是什么？", options: ["检查结果是否只依赖一个精确参数", "把收益调到最高", "删掉亏损年份"], correct: 0, explanation: "附近参数也能工作，通常比单点最优更稳健。" },
    { question: "如果加上更高滑点后策略立刻亏损，说明什么？", options: ["策略对交易成本敏感", "滑点不应计入", "策略已经证明有效"], correct: 0, explanation: "成本敏感意味着实盘成交条件和容量需要进一步评估。" },
    { question: "为什么要按上涨、下跌、震荡阶段分别看结果？", options: ["识别策略适用的市场环境", "只为了增加图表数量", "避免查看最大回撤"], correct: 0, explanation: "分阶段能发现策略是否只在某一种行情中有效。" },
    { question: "反复尝试很多因子后只保留表现最好的一个，主要风险是什么？", options: ["多重试验导致偶然最优，容易过拟合", "数据一定更完整", "回测会自动变成样本外"], correct: 0, explanation: "尝试越多，越容易碰巧找到历史上最好的规则，需要独立验证。" },
  ],
};

function Guide({ open, onContextChange }: { open: (view: View) => void; onContextChange?: (context: ResearchContext) => void }) {
  const progressKey = "quantweave-learning-progress-v1";
  const [active, setActive] = useState(0);
  const [completed, setCompleted] = useState<string[]>([]);
  const [questionIndexes, setQuestionIndexes] = useState<Record<string, number>>({});
  const [answers, setAnswers] = useState<Record<string, Record<number, number>>>({});
  const lesson = learningLessons[active];
  const exercises = [lesson.exercise, ...lesson.moreExercises, ...(additionalLearningExercises[lesson.id] || [])];
  const questionIndex = questionIndexes[lesson.id] || 0;
  const exercise = exercises[questionIndex];
  const answer = answers[lesson.id]?.[questionIndex];
  const answered = answer !== undefined;
  const correct = answer === exercise.correct;
  const passedCount = exercises.filter((item, index) => answers[lesson.id]?.[index] === item.correct).length;
  const mastered = passedCount === exercises.length || completed.includes(lesson.id);

  useEffect(() => {
    let restored: string[] = [];
    try {
      const saved = JSON.parse(window.localStorage.getItem(progressKey) || "[]");
      if (Array.isArray(saved)) restored = saved.filter(value => learningLessons.some(item => item.id === value));
    } catch {
      window.localStorage.removeItem(progressKey);
    }
    const restoreProgress = window.setTimeout(() => setCompleted(restored), 0);
    return () => window.clearTimeout(restoreProgress);
  }, []);

  const markComplete = () => {
    if (!mastered) return;
    const next = completed.includes(lesson.id) ? completed : [...completed, lesson.id];
    setCompleted(next);
    window.localStorage.setItem(progressKey, JSON.stringify(next));
    if (active < learningLessons.length - 1) setActive(active + 1);
  };

  useEffect(() => {
    onContextChange?.({
      surface: "guide",
      title: `学习中心 · 第 ${active + 1} 节 ${lesson.title}`,
      summary: active === 2 ? "正在学习如何把股票池、硬筛、因子、组合和执行节点连成第一条工作流。" : lesson.summary,
      details: [`本节掌握进度 ${passedCount}/${exercises.length}`, `当前练习 ${questionIndex + 1}/${exercises.length}`],
      suggestions: ["用最简单的话解释我当前这一节", "把当前课程内容变成一条工作流", "给我一个类似当前知识点的练习"],
    });
  }, [active, exercises.length, lesson.id, lesson.summary, lesson.title, onContextChange, passedCount, questionIndex]);

  return <main className="bt-content bt-learning">
    <header className="bt-learning-hero"><div><span>LEARNING CENTER</span><h1>量化回测学习路径</h1><p>跟着当前任务一步一步做，不需要先学编程。</p></div><div className="bt-learning-progress"><span>总进度</span><strong>{completed.length}<small> / {learningLessons.length} 节</small></strong><i><b style={{ width: `${completed.length / learningLessons.length * 100}%` }}/></i></div></header>
    <section className="bt-current-mission"><span>当前任务</span><div><b>完成第 {active + 1} 节：{lesson.title}</b><small>{active === 2 ? "先看五步搭建示范，再进入画布照着连接。" : "先看本节例子，再完成 10 道练习。"}</small></div><strong>{passedCount}<small> / {exercises.length} 题</small></strong></section>
    <div className="bt-learning-workspace">
      <nav className="bt-lesson-nav" aria-label="课程目录">{learningLessons.map((item, index) => <button key={item.id} className={`${index === active ? "active" : ""} ${completed.includes(item.id) ? "done" : ""}`} onClick={() => setActive(index)}><span>{completed.includes(item.id) ? <CheckCircle2/> : String(index + 1).padStart(2, "0")}</span><div><b>{item.title}</b><small>{completed.includes(item.id) ? "已掌握" : "10 道掌握练习"}</small></div><ChevronRight/></button>)}</nav>
      <article className="bt-lesson" key={lesson.id}>
        <header><span>当前课程 · 第 {active + 1} 节</span><h2>{lesson.title}</h2><p><b>本节重点：</b>{lesson.goal}</p></header>
        <section className="bt-lesson-concepts"><h3>先理解这几个词</h3><dl>{lesson.concepts.map(item => <div key={item.name}><dt>{item.name}</dt><dd>{item.detail}</dd></div>)}</dl></section>
        <section className="bt-lesson-example"><span>例子</span><h3>{lesson.example.title}</h3><div><p>{lesson.example.before}</p><ArrowRight/><code>{lesson.example.after}</code></div><small><Info/>{lesson.example.note}</small></section>
        {lesson.id === "workflow" && <section className="bt-workflow-tutorial"><header><span>手把手搭建</span><h3>第一条可运行工作流</h3><p>先严格照着搭一遍，成功运行后再替换成自己的指标。</p></header><ol><li><span>1</span><div><b>拖入“沪深 A 股”股票池</b><small>它决定从哪些股票里开始筛选。先不要使用自定义代码池。</small></div></li><li><span>2</span><div><b>连接硬筛节点</b><small>设置排除 ST、停牌、上市不足 120 日和平均成交额过低的股票。</small></div></li><li><span>3</span><div><b>连接“20 日动量”因子</b><small>选择降序排列，让最近 20 日涨幅更强的股票排在前面。</small></div></li><li><span>4</span><div><b>连接组合节点</b><small>持仓数填 5，权重选择等权，调仓频率选择每周。</small></div></li><li><span>5</span><div><b>连接 A 股执行节点并运行</b><small>选择次日执行，保留涨跌停、停牌、整手、佣金和滑点限制。</small></div></li></ol><footer><button onClick={() => open("studio")}><Boxes/>现在去画布照着搭<ArrowRight/></button></footer></section>}
        <section className="bt-lesson-exercise"><div className="bt-exercise-head"><span>掌握练习</span><nav aria-label="本节题目">{exercises.map((item, index) => { const passed = answers[lesson.id]?.[index] === item.correct; return <button key={item.question} className={`${index === questionIndex ? "active" : ""} ${passed ? "passed" : ""}`} aria-label={`第 ${index + 1} 题${passed ? "，已答对" : ""}`} onClick={() => setQuestionIndexes(current => ({ ...current, [lesson.id]: index }))}>{passed ? <CheckCircle2/> : index + 1}</button>; })}</nav></div><h3>{exercise.question}</h3><div className="bt-exercise-options">{exercise.options.map((option, index) => <button key={option} className={answered && index === exercise.correct ? "correct" : answered && index === answer ? "wrong" : ""} onClick={() => setAnswers(current => ({ ...current, [lesson.id]: { ...current[lesson.id], [questionIndex]: index } }))}><i>{String.fromCharCode(65 + index)}</i>{option}{answered && index === exercise.correct && <CheckCircle2/>}</button>)}</div>{answered && <p className={correct ? "correct" : "wrong"}>{correct ? "回答正确。" : "再看一下上面的概念。"} {exercise.explanation}</p>}{correct && questionIndex < exercises.length - 1 && <div className="bt-exercise-next"><button onClick={() => setQuestionIndexes(current => ({ ...current, [lesson.id]: questionIndex + 1 }))}>下一题<ChevronRight/></button></div>}{passedCount === exercises.length && <p className="mastered"><CheckCircle2/>本节 {exercises.length} 道题全部答对，可以标记为已掌握。</p>}</section>
        <footer><button onClick={() => setActive(Math.max(0, active - 1))} disabled={active === 0}>上一节</button>{lesson.action && <button onClick={() => open(lesson.action!.view)}><Play/>{lesson.action.label}</button>}<button className="primary" onClick={markComplete} disabled={!mastered}>{completed.includes(lesson.id) ? "已掌握，下一节" : `完成本节（${passedCount}/${exercises.length}）`}<ChevronRight/></button></footer>
      </article>
    </div>
  </main>;
}

export default function BacktestSite({ initialView }: { initialView?: string } = {}) {
  const firstView = resolveView(initialView);
  const [view, setView] = useState<View>(firstView);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [catalog, setCatalog] = useState<CatalogPayload | null>(null);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [market, setMarket] = useState<MarketStatus | null>(null);
  const [scheduler, setScheduler] = useState<SchedulerStatus | null>(null);
  const [services, setServices] = useState<Record<string, ServiceState>>({ quant: { status: "checking" }, lab: { status: "checking" } });
  const [researchContext, setResearchContext] = useState<ResearchContext>(() => contextForView(firstView));
  const [aiDockOpen, setAiDockOpen] = useState(false);
  const handleContextChange = useCallback((context: ResearchContext) => setResearchContext(context), []);

  const open = useCallback((next: View) => {
    setView(next); setResearchContext(contextForView(next)); setMobileOpen(false); if (!["studio", "factors", "guide"].includes(next)) setAiDockOpen(false); window.scrollTo({ top: 0 });
    const url = next === "overview" ? "/backtest" : `/backtest?view=${next}`;
    window.history.replaceState({}, "", url);
  }, []);

  const checkServices = useCallback(async () => {
    setServices(current => Object.fromEntries(Object.keys(current).map(key => [key, { status: "checking" }])));
    try {
      const payload = await getJson<{ services: Record<string, ServiceState> }>("/api/backtest-health");
      setServices(payload.services);
    } catch (reason) {
      const detail = reason instanceof Error ? reason.message : "健康检查接口不可用";
      setServices({ quant: { status: "offline", detail }, lab: { status: "offline", detail } });
    }
  }, []);
  const refreshScheduler = useCallback(async () => {
    try {
      setScheduler(await getJson<SchedulerStatus>("/api/data-scheduler-status"));
    } catch {
      setScheduler(null);
    }
  }, []);

  useEffect(() => {
    const requested = resolveView(new URLSearchParams(window.location.search).get("view"));
    if (requested !== view) window.setTimeout(() => { setView(requested); setResearchContext(contextForView(requested)); }, 0);
    Promise.allSettled([
      getJson<CatalogPayload>(`${API}/api/v1/lego/catalog`).then(setCatalog),
      getJson<{ runs: RunSummary[] }>(`${API}/api/v1/lego/runs?limit=100`).then(payload => {
        const rows = payload.runs || [];
        setRuns(rows);
      }),
      getJson<MarketStatus>("/data/market-data-status.json").then(setMarket),
    ]);
    const initialHealthCheck = window.setTimeout(() => { void checkServices(); }, 0);
    const initialSchedulerCheck = window.setTimeout(() => { void refreshScheduler(); }, 0);
    const timer = window.setInterval(() => { void checkServices(); void refreshScheduler(); }, 60000);
    return () => { window.clearTimeout(initialHealthCheck); window.clearTimeout(initialSchedulerCheck); window.clearInterval(timer); };
  }, [checkServices, refreshScheduler, view]);

  const activeNav = navItems.find(item => item.id === view) || navItems[0];
  const contextAwareView = ["studio", "factors", "guide"].includes(view);
  return <div className={`bt-site view-${view}`}>
    <header className="bt-site-header"><a className="bt-brand" href="/backtest" aria-label="量构首页"><span><LineChart/></span><div><b>量构</b><small>QUANTWEAVE</small></div></a><button className="bt-mobile-menu" onClick={() => setMobileOpen(value => !value)} aria-label="打开导航">{mobileOpen ? <X/> : <Menu/>}</button><nav className={mobileOpen ? "open" : ""}>{navItems.map(item => { const Icon = item.icon; return <button key={item.id} className={view === item.id ? "active" : ""} onClick={() => open(item.id)} title={item.hint}><Icon/><span>{item.label}</span></button>; })}</nav><div className="bt-header-actions"><span><StatusDot state={services.quant?.status || "checking"}/>{services.quant?.status === "online" ? "回测引擎在线" : "引擎未连接"}</span></div></header>
    <div className="bt-mobile-context"><activeNav.icon/><span>{activeNav.label}</span><small>{activeNav.hint}</small></div>
    {view === "overview" && <Overview catalog={catalog} runs={runs} market={market} service={services.quant || { status: "checking" }} open={open}/>} 
    {view === "studio" && <div className="bt-embedded-tool"><QuantWorkflowStudio onContextChange={handleContextChange}/></div>}
    {view === "factors" && <div className="bt-embedded-tool"><FactorLab onContextChange={handleContextChange}/></div>}
    {view === "ai" && <div className="bt-embedded-tool"><AiWorkspace context={researchContext}/></div>}
    {view === "runs" && <RunRecords runs={runs}/>} 
    {view === "data" && <DataCenter market={market} scheduler={scheduler}/>} 
    {view === "guide" && <Guide open={open} onContextChange={handleContextChange}/>} 
    {contextAwareView && <><button className={`bt-ai-dock-trigger ${aiDockOpen ? "active" : ""}`} onClick={() => setAiDockOpen(value => !value)}><Bot/><span>AI 助手</span><small>{researchContext.title}</small></button>{aiDockOpen && <aside className="bt-ai-dock"><AiWorkspace embedded context={researchContext} onClose={() => setAiDockOpen(false)}/></aside>}</>}
 
    {!(["studio", "factors", "ai"] as View[]).includes(view) && <footer className="bt-footer"><div><span><LineChart/></span><b>量构 QuantWeave</b><small>低代码 A 股量化研究</small></div><p>仅用于研究与模拟回测，不构成投资建议，不连接实盘交易。</p><nav><button onClick={() => open("guide")}>使用指南</button></nav></footer>}
  </div>;
}
