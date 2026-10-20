"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  addEdge,
  Background,
  BackgroundVariant,
  Connection,
  Controls,
  Edge,
  Handle,
  MarkerType,
  MiniMap,
  Node,
  NodeProps,
  Position,
  ReactFlow,
  ReactFlowInstance,
  useEdgesState,
  useNodesState,
} from "@xyflow/react";
import {
  Activity,
  Box,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  CirclePlay,
  Copy,
  Database,
  Download,
  FilePlus2,
  FileUp,
  GitBranch,
  GripVertical,
  History,
  Lightbulb,
  LoaderCircle,
  Play,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  Settings2,
  Trash2,
  Upload,
  WandSparkles,
  X,
  ZoomIn,
} from "lucide-react";
import MatureKlineChart from "./kline-chart";
import type { ResearchContext } from "./research-context";

const workflowApi = () => "/api/v1/lego";
const STAGES = [
  "universe",
  "filter",
  "strategy",
  "factor",
  "normalize",
  "penalty",
  "rank",
  "portfolio",
  "execution",
] as const;
const STAGE_META: Record<
  string,
  { label: string; color: string; input: string; output: string }
> = {
  universe: {
    label: "01 数据源",
    color: "#2d8294",
    input: "",
    output: "股票池",
  },
  filter: {
    label: "02 硬筛",
    color: "#5264a6",
    input: "股票池",
    output: "候选股",
  },
  strategy: {
    label: "03 策略初选",
    color: "#7554a4",
    input: "行情",
    output: "信号",
  },
  factor: {
    label: "04 因子与模型",
    color: "#9b6538",
    input: "候选股",
    output: "因子值",
  },
  normalize: {
    label: "05 标准化",
    color: "#89752d",
    input: "原始分",
    output: "标准分",
  },
  penalty: {
    label: "06 风险惩罚",
    color: "#9a4c4c",
    input: "标准分",
    output: "最终分",
  },
  rank: { label: "07 排名", color: "#3e8068", input: "最终分", output: "排名" },
  portfolio: {
    label: "08 组合",
    color: "#28766e",
    input: "排名",
    output: "目标仓位",
  },
  execution: {
    label: "09 回测执行",
    color: "#2d679e",
    input: "目标仓位",
    output: "回测结果",
  },
};

type BlockSpec = {
  id: string;
  type: string;
  category: string;
  name: string;
  enabled: boolean;
  combine: "AND" | "OR";
  params: Record<string, unknown>;
};
type BlockMeta = {
  category: string;
  name: string;
  description: string;
  params: Record<string, unknown>;
  available?: boolean;
  disabled_reason?: string;
  required_data?: string[];
  base_type?: string;
  template_id?: string;
  user_template?: boolean;
  family?: string;
  formula?: string;
  required_fields?: string[];
  output?: string;
  point_in_time?: boolean;
  plain_explanation?: string;
  how_to_read?: string;
  best_for?: string;
  pitfall?: string;
  example_caption?: string;
  rule_version?: string;
  rule_status?: string;
  rule_source?: string;
  rule_source_url?: string;
  rule_items?: string[];
  rule_notes?: string[];
};
type Pipeline = {
  id: string;
  name: string;
  description: string;
  version: number;
  revision?: number;
  scope: string;
  start: string;
  end: string;
  initial_cash: number;
  blocks: BlockSpec[];
  fees: Record<string, number>;
  workflow?: {
    nodes: Array<{ id: string; position: { x: number; y: number } }>;
    edges: Array<{
      id: string;
      source: string;
      target: string;
      label?: string;
    }>;
  };
};
type QuantNodeData = { block: BlockSpec; meta: BlockMeta } & Record<
  string,
  unknown
>;
type QuantNode = Node<QuantNodeData, "quant">;
type JsonRecord = Record<string, string | number | boolean | null | undefined>;
const errorMessage = (reason: unknown) => reason instanceof Error ? reason.message : String(reason);
function normalizeBlock(block: BlockSpec): BlockSpec {
  return block;
}
type BacktestResult = {
  run_id: string;
  metrics: Record<string, number>;
  coverage: JsonRecord;
  equity: JsonRecord[];
  trades: JsonRecord[];
  closed_trades: JsonRecord[];
  stock_pnl: JsonRecord[];
  stage_funnel: JsonRecord[];
  /** Full rank detail rows returned by the service.  rank_preview is kept for older payloads. */
  rank_details?: JsonRecord[];
  rank_preview: JsonRecord[];
  factor_analysis: JsonRecord[];
  factor_groups: JsonRecord[];
  factor_correlations: JsonRecord[];
};
type SavedPipeline = {
  id: string;
  name: string;
  description?: string;
  revision?: number;
  blocks?: number;
  updated_at?: string;
  runnable?: boolean;
  validation_error?: string;
};
type RunSummary = {
  run_id: string;
  created_at: string;
  pipeline_id?: string;
  pipeline_name?: string;
  start?: string;
  end?: string;
  metrics: Record<string, number>;
};

const stageIndex = (stage: string) =>
  Math.max(0, STAGES.indexOf(stage as (typeof STAGES)[number]));
const newId = (type: string) => `${type}-${crypto.randomUUID().slice(0, 8)}`;
const isCustomFormulaBlock = (type: string) =>
  type === "custom_formula" || type.endsWith("_custom_formula");
const PARAM_VALUE_LABELS: Record<string, string> = {
  next_open: "次日开盘",
  equal: "等权",
  higher: "越大越好",
  lower: "越小越好",
  percentile: "横截面百分位",
  robust_sigmoid: "稳健S形",
  median_mad: "中位数/MAD",
  disabled: "未启用",
  pending: "待验证",
  all: "沪深京全市场",
  sh_sz: "沪深A股",
  sh: "上证A股",
  sz: "深证A股",
  bj: "北交所",
  main: "沪深主板",
  star: "科创板",
  chinext: "创业板",
  etf: "ETF",
  custom: "自定义代码池",
  sse50: "上证50历史成分",
  csi300: "沪深300历史成分",
  csi500: "中证500历史成分",
  csi1000: "中证1000历史成分",
  csi_a500: "中证A500历史成分",
  same_day_1445: "当日14:45执行",
  hard_non_st: "剔除ST",
  hard_tradable: "可交易检查",
  hard_limit: "涨停检查",
  hard_listing: "上市天数检查",
  hard_liquidity: "流动性检查",
  market_gate: "市场门控",
  strategy: "策略条件",
  custom_filter: "自定义筛选",
  custom_strategy: "自定义策略",
};
const formatParam = (value: unknown) =>
  typeof value === "boolean"
    ? value ? "是" : "否"
    : typeof value === "string" && PARAM_VALUE_LABELS[value]
      ? PARAM_VALUE_LABELS[value]
      : typeof value === "number"
    ? Math.abs(value) < 0.01 && value !== 0
      ? value.toFixed(5)
      : String(value)
    : Array.isArray(value)
      ? `${value.length}项`
      : typeof value === "object"
        ? "配置"
        : String(value ?? "--");
const PARAM_LABELS: Record<string, string> = {
  name: "因子名称",
  formula: "因子公式",
  weight: "权重",
  direction: "方向",
  method: "标准化",
  lookback: "回看周期",
  window: "窗口周期",
  period: "周期",
  start_one: "单日开启阈值(%)",
  use_two_day: "启用两日条件",
  start_two: "两日开启阈值(%)",
  exit_one: "关闭跌幅阈值（≤，与M线为OR）",
  ma_period: "关闭M线周期（跌破即关闭）",
  ma_buffer: "M线关闭缓冲比例",
  max_gap_days: "数据最大缺口(天)",
  j_threshold: "KDJ J值上限",
  min_gain: "B1最小涨跌幅(%)",
  max_gain: "B1最大涨跌幅(%)",
  main_max_amplitude: "主板最大振幅(%)",
  growth_max_amplitude: "双创/北交最大振幅(%)",
  max_volume_ratio: "最大量比(相对前日)",
  n_lift_floor: "N型低点比例下限",
  require_non_green_brick: "禁止绿砖开仓",
  require_white_above_yellow: "要求白线不低于黄线",
  require_support: "要求守住MA60或黄线",
  require_macd_positive: "MACD DIF零轴过滤",
  require_no_s1: "禁止S1放量大阴",
  fast: "短周期",
  medium: "中周期",
  slow: "长周期",
  short: "短周期",
  long: "长周期",
  slope_days: "斜率间隔",
  stddev: "标准差倍数",
  min_days: "最少上市天数",
  min_amount: "最低平均成交额(元)",
  top_k: "每日候选Top K",
  positions: "组合持仓数",
  rebalance_every: "调仓间隔(交易日)",
  invest_ratio: "组合总仓位上限",
  max_position_pct: "单票仓位上限",
  max_participation: "单日成交量参与上限",
  provider: "数据来源",
  mode: "执行方式",
  commission: "佣金率",
  min_commission: "最低佣金(元)",
  transfer_fee: "过户费率",
  stamp_tax: "印花税率",
  slippage: "滑点率",
  lot_size: "每手股数",
  min_return: "最低收益要求",
  min_value: "最小值",
  max_value: "最大值",
  min_gap: "最小间隔",
  max_gap: "最大间隔",
  entry: "入场条件",
  time: "执行时间",
  short_period: "短线周期",
  long_period: "长线周期",
  short_floor: "短线下限",
  long_floor: "长线下限",
  sigmoid_k: "S形变换斜率",
  clip: "极值截断倍数",
  winsorize: "去极值方法",
  strategy_id: "本地策略",
  model: "模型状态",
  model_id: "模型编号",
  train_window: "训练窗口(交易日)",
  retrain_every: "重新训练间隔",
  params: "模型参数",
  formula_version: "公式版本",
  codes: "股票代码列表",
  group: "内部逻辑组",
  scope: "股票范围",
  weighting: "持仓权重方式",
  allow_b1: "允许B1信号",
  allow_b2: "允许B2信号",
  allow_b3: "允许B3信号",
  forbid_s1: "排除S1风险",
  forbid_green_flip: "排除红砖翻绿",
  forbid_white_dead_cross: "排除白线死叉黄线",
  forbid_below_yellow: "排除跌破黄线",
};
const paramLabel = (name: string) => PARAM_LABELS[name] || name;
const formatMetric = (key: string, value: number | undefined) => {
  if (value === undefined || value === null || !Number.isFinite(value))
    return "--";
  if (
    [
      "total_return",
      "cagr",
      "max_drawdown",
      "annual_volatility",
      "win_rate",
    ].includes(key)
  )
    return `${(value * 100).toFixed(2)}%`;
  if (key === "final_equity")
    return `¥${value.toLocaleString("zh-CN", { maximumFractionDigits: 0 })}`;
  return value.toFixed(2);
};
const formatRatio = (value: unknown, digits = 2) => {
  const numeric = Number(value);
  return Number.isFinite(numeric)
    ? `${(numeric * 100).toFixed(digits)}%`
    : "--";
};

const rankDate = (row: JsonRecord) => {
  const value = row.signal_date ?? row.trade_date ?? row.date;
  return value == null || value === "" ? "" : String(value).slice(0, 10);
};

const rankingRowsFor = (result: BacktestResult) => {
  const full = result.rank_details || [];
  return full.length ? full : result.rank_preview || [];
};

const rankingTotalFor = (result: BacktestResult, rows: JsonRecord[]) => {
  const reported = Number(
    result.coverage?.rank_detail_rows ??
      result.coverage?.rank_rows ??
      result.coverage?.candidate_rows,
  );
  return Number.isFinite(reported) && reported >= 0 ? reported : rows.length;
};

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${workflowApi()}${path}`, {
    cache: "no-store",
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "工作流服务请求失败");
  return payload;
}

function QuantNodeView({ data, selected }: NodeProps<QuantNode>) {
  const { block, meta } = data;
  const stage = STAGE_META[block.category] || STAGE_META.strategy;
  const unavailable = meta.available === false || Boolean(meta.disabled_reason);
  const params = Object.entries(block.params || {})
    .filter(([key]) => !["group", "codes", "name"].includes(key))
    .slice(0, 5);
  const displayName = block.name;
  return (
    <div
      className={`qnode ${selected ? "selected" : ""} ${!block.enabled ? "disabled" : ""}`}
      style={{ "--node-color": stage.color } as React.CSSProperties}
    >
      {block.category !== "universe" && (
        <Handle
          type="target"
          position={Position.Left}
          id="input"
          className="qnode-handle input"
        />
      )}
      <div className="qnode-head">
        <span />
        <b>{displayName}</b>
        <Settings2 size={13} />
      </div>
      <div className="qnode-port-label input-label">{stage.input}</div>
      <div className="qnode-body">
        <small>{meta.description}</small>
        {params.map(([key, value]) => (
          <div className="qnode-param" key={key}>
            <span>{paramLabel(key)}</span>
            <b>{formatParam(value)}</b>
          </div>
        ))}
        {!params.length && <div className="qnode-empty">无需参数</div>}
      </div>
      <div className="qnode-status">
        <i className={unavailable ? "warn" : block.enabled ? "ready" : "off"} />
        {unavailable ? "数据未就绪" : block.enabled ? "等待运行" : "已停用"}
      </div>
      <div className="qnode-port-label output-label">{stage.output}</div>
      {block.category !== "execution" && (
        <Handle
          type="source"
          position={Position.Right}
          id="output"
          className="qnode-handle output"
        />
      )}
    </div>
  );
}

const NODE_TYPES = { quant: QuantNodeView };

function pipelineGraph(
  pipeline: Pipeline,
  catalog: Record<string, BlockMeta>,
): { nodes: QuantNode[]; edges: Edge[] } {
  const positions = Object.fromEntries(
    (pipeline.workflow?.nodes || []).map((node) => [node.id, node.position]),
  );
  const rows: Record<string, number> = {};
  const nodes = pipeline.blocks.map((rawBlock) => {
    const block = normalizeBlock(rawBlock);
    const row = rows[block.category] || 0;
    rows[block.category] = row + 1;
    return {
      id: block.id,
      type: "quant" as const,
      position: positions[block.id] || {
        x: 90 + stageIndex(block.category) * 280,
        y: 80 + row * 170,
      },
      data: {
        block,
        meta: catalog[block.type] || {
          category: block.category,
          name: block.name,
          description: "自定义模块",
          params: block.params,
        },
      },
    };
  });
  const providedEdges = pipeline.workflow?.edges || [];
  const connectedIds = new Set(providedEdges.flatMap((edge) => [edge.source, edge.target]));
  const graphIsComplete = pipeline.blocks.length <= 1 || (
    providedEdges.length >= pipeline.blocks.length - 1 &&
    pipeline.blocks.every((block) => connectedIds.has(block.id))
  );
  const sourceEdges = graphIsComplete
    ? providedEdges
    : pipeline.blocks.slice(0, -1).map((block, index) => ({
        id: `edge-${block.id}-${pipeline.blocks[index + 1].id}`,
        source: block.id,
        target: pipeline.blocks[index + 1].id,
        label: "数据流",
      }));
  const edges = sourceEdges.map((edge) => ({
    ...edge,
    type: "smoothstep",
    animated: true,
    markerEnd: { type: MarkerType.ArrowClosed, color: "#72c7ab" },
    style: { stroke: "#72c7ab", strokeWidth: 2 },
    labelStyle: { fill: "#8da29b", fontSize: 10 },
  }));
  return { nodes, edges };
}

type FormulaStatus = {
  state: "idle" | "checking" | "valid" | "error";
  message: string;
};

function inferFormulaSuggestion(input: string): string | null {
  const original = input.trim();
  if (!original) return null;
  let candidate = original
    .replaceAll("（", "(")
    .replaceAll("）", ")")
    .replaceAll("，", ",")
    .replaceAll("＞", ">")
    .replaceAll("＜", "<")
    .replaceAll("＝", "=")
    .replaceAll("！", "!")
    .replace(/&&/g, " and ")
    .replace(/\|\|/g, " or ")
    .replace(/<>/g, " != ")
    .replace(/\bAND\b/gi, "and")
    .replace(/\bOR\b/gi, "or")
    .replace(/\bNOT\b/gi, "not")
    .replace(/并且|而且|且/g, " and ")
    .replace(/或者|或/g, " or ");
  const fieldAliases: Array<[RegExp, string]> = [
    [/\bCLOSE\b/gi, "close"],
    [/\bOPEN\b/gi, "open"],
    [/\bHIGH\b/gi, "high"],
    [/\bLOW\b/gi, "low"],
    [/\bVOLUME\b/gi, "volume"],
    [/\bAMOUNT\b/gi, "amount"],
    [/\bC\b/g, "close"],
    [/\bO\b/g, "open"],
    [/\bH\b/g, "high"],
    [/\bL\b/g, "low"],
    [/\bV\b/g, "volume"],
  ];
  for (const [pattern, replacement] of fieldAliases)
    candidate = candidate.replace(pattern, replacement);
  for (const name of ["MOM", "MA", "RSI", "VOL", "ADV", "DRAWDOWN"])
    candidate = candidate.replace(
      new RegExp(`\\b${name}\\s*(\\d{1,3})\\b`, "gi"),
      `${name}($1)`,
    );
  candidate = candidate
    .replace(/(?<![<>=!])=(?!=)/g, "==")
    .replace(/(\d+(?:\.\d+)?)\s*%/g, (_, number) =>
      String(Number(number) / 100),
    )
    .replace(/\s+/g, " ")
    .trim();
  const openCount = (candidate.match(/\(/g) || []).length;
  const closeCount = (candidate.match(/\)/g) || []).length;
  if (openCount > closeCount) candidate += ")".repeat(openCount - closeCount);
  return candidate !== original ? candidate : null;
}

function FormulaInput({
  value,
  blockType,
  onChange,
}: {
  value: string;
  blockType: string;
  onChange: (value: string) => void;
}) {
  const [status, setStatus] = useState<FormulaStatus>({
    state: "idle",
    message: "",
  });
  useEffect(() => {
    const formula = value.trim();
    if (!formula) return;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setStatus({ state: "checking", message: "正在检查语法" });
      try {
        const payload = await api<{ mode: "condition" | "numeric" }>(
          "/validate-formula",
          {
            method: "POST",
            signal: controller.signal,
            body: JSON.stringify({ formula, block_type: blockType }),
          },
        );
        setStatus({
          state: "valid",
          message:
            payload.mode === "condition"
              ? "语法正确，可以作为筛选条件"
              : "语法正确，可以作为数值因子",
        });
      } catch (reason: unknown) {
        if (!controller.signal.aborted)
          setStatus({ state: "error", message: errorMessage(reason) });
      }
    }, 350);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [blockType, value]);
  const displayStatus = value.trim() ? status : { state: "idle" as const, message: "请输入公式" };
  const suggestion =
    displayStatus.state === "error" ? inferFormulaSuggestion(value) : null;
  return (
    <div className="qflow-formula-editor">
      <textarea
        className="qflow-formula-input"
        aria-label="因子公式"
        value={value}
        placeholder="例如：close > MA(20) and MOM(5) > 0"
        spellCheck={false}
        onChange={(event) => onChange(event.target.value)}
      />
      <div className={`qflow-formula-status ${displayStatus.state}`}>
        {displayStatus.state === "error" ? (
          <CircleAlert />
        ) : displayStatus.state === "valid" ? (
          <Check />
        ) : (
          <LoaderCircle className={displayStatus.state === "checking" ? "spin" : ""} />
        )}
        <span>{displayStatus.message}</span>
      </div>
      {suggestion && (
        <button
          type="button"
          className="qflow-formula-suggestion"
          onClick={() => onChange(suggestion)}
        >
          <WandSparkles />
          <span>
            <b>猜你想输入</b>
            <code>{suggestion}</code>
          </span>
          <em>采用</em>
        </button>
      )}
      <details className="qflow-formula-help">
        <summary>
          <Lightbulb />
          公式语法
        </summary>
        <div>
          <p>
            <b>字段</b>{" "}
            close、open、high、low、volume、amount、prev_close、daily_return、listing_days
          </p>
          <p>
            <b>周期函数</b>{" "}
            MA(20)、MOM(20)、RSI(14)、VOL(20)、ADV(20)、DRAWDOWN(60)
          </p>
          <p>
            <b>数学函数</b> ABS(x)、LOG(x)、SQRT(x)、MIN(a,b)、MAX(a,b)
          </p>
          <p>
            <b>条件</b> 使用 &gt;、&gt;=、&lt;、&lt;=、==、!=，多个条件用
            and、or、not
          </p>
          <code>close &gt; MA(20) and MOM(5) &gt; 0</code>
          <code>MOM(20) / (VOL(20) + 0.001)</code>
        </div>
      </details>
    </div>
  );
}

function ParamInput({
  name,
  value,
  blockType = "",
  onChange,
}: {
  name: string;
  value: unknown;
  blockType?: string;
  onChange: (value: unknown) => void;
}) {
  const options: Record<string, Array<[string, string]>> = {
    direction: [
      ["higher", "越大越好"],
      ["lower", "越小越好"],
    ],
    method: [
      ["robust_sigmoid", "稳健S形"],
      ["percentile", "百分位"],
    ],
    combine: [
      ["AND", "同时满足 AND"],
      ["OR", "满足任一 OR"],
    ],
    scope: [
      ["all", "沪深京全市场"],
      ["sh_sz", "沪深 A 股"],
      ["sh", "上证 A 股"],
      ["sz", "深证 A 股"],
      ["bj", "北交所"],
      ["main", "沪深主板"],
      ["star", "科创板"],
      ["chinext", "创业板"],
      ["etf", "ETF"],
      ["custom", "自定义代码池"],
    ],
    weighting: [["equal", "等权"]],
    mode: [["next_open", "次日开盘执行"]],
    winsorize: [["median_mad", "中位数/MAD去极值"]],
  };
  if (options[name])
    return (
      <select
        value={String(value)}
        onChange={(event) => onChange(event.target.value)}
      >
        {options[name].map(([key, label]) => (
          <option value={key} key={key}>
            {label}
          </option>
        ))}
      </select>
    );
  if (name === "formula")
    return (
      <FormulaInput
        value={String(value || "")}
        blockType={blockType}
        onChange={onChange}
      />
    );
  if (typeof value === "boolean")
    return (
      <button
        className={`qflow-switch ${value ? "on" : ""}`}
        onClick={() => onChange(!value)}
      >
        <i />
      </button>
    );
  if (typeof value === "number")
    return (
      <input
        type="number"
        value={value}
        step={
          Number.isInteger(value) ? 1 : Math.abs(value) < 0.01 ? 0.0001 : 0.01
        }
        onChange={(event) => onChange(Number(event.target.value))}
      />
    );
  if (Array.isArray(value))
    return (
      <textarea
        value={value.join(", ")}
        onChange={(event) =>
          onChange(
            event.target.value
              .split(/[,，]/)
              .map((item) => item.trim())
              .filter(Boolean),
          )
        }
      />
    );
  if (typeof value === "object" && value !== null)
    return <textarea value={JSON.stringify(value, null, 2)} readOnly />;
  return (
    <input
      value={String(value ?? "")}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

type FactorExampleCandle = {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  amount: number;
  factor_value: number | null;
  factor_score: number | null;
};
type FactorExampleResult = {
  factor_type: string;
  factor_name: string;
  code: string;
  name: string;
  source: string;
  as_of: string;
  chart_from?: string;
  chart_to?: string;
  direction: "higher" | "lower";
  candles: FactorExampleCandle[];
  example?: {
    date: string;
    factor_value: number;
    close: number;
    forward_5_return: number;
    note: string;
  } | null;
  statistics: Record<string, number | null>;
  teaching: Record<string, string | null>;
};

const factorNumber = (value: number | null | undefined, digits = 4) =>
  Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "--";

const teachingAverage = (values: number[], period: number) => {
  const result = Array<number | null>(values.length).fill(null);
  let sum = 0;
  for (let index = 0; index < values.length; index += 1) {
    sum += values[index];
    if (index >= period) sum -= values[index - period];
    if (index >= period - 1) result[index] = sum / period;
  }
  return result;
};

function FactorAnnotatedExample({
  rows,
  event,
  factorName,
  block,
}: {
  rows: FactorExampleCandle[];
  event?: FactorExampleResult["example"];
  factorName: string;
  block: BlockSpec;
}) {
  const eventIndex = event ? rows.findIndex((row) => row.date === event.date) : -1;
  const windowStart = Math.max(
    0,
    Math.min(rows.length - 72, eventIndex >= 0 ? eventIndex - 46 : rows.length - 72),
  );
  const visible = rows.slice(windowStart, Math.min(rows.length, windowStart + 72));
  const visibleEventIndex = eventIndex - windowStart;
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  if (!visible.length)
    return <div className="qflow-factor-example-empty">当前区间没有可绘制的行情</div>;

  const width = 960;
  const height = 500;
  const left = 54;
  const right = 54;
  const priceTop = 50;
  const priceBottom = 300;
  const factorTop = 352;
  const factorBottom = 454;
  const plotWidth = width - left - right;
  const x = (index: number) =>
    left + (index + 0.5) * (plotWidth / Math.max(1, visible.length));
  const candleWidth = Math.max(2.5, Math.min(9, (plotWidth / visible.length) * 0.62));
  const period = Math.max(
    2,
    Number(block.params.period || block.params.window || block.params.lookback || 20),
  );
  const fast = Math.max(2, Number(block.params.fast || Math.min(5, period)));
  const slow = Math.max(
    period + 1,
    Number(block.params.slow || block.params.long || Math.max(20, period * 2)),
  );
  const periods = [...new Set([fast, period, slow])].slice(0, 3);
  const allCloses = rows.map((row) => row.close);
  const averages = periods.map((value) => ({
    period: value,
    values: teachingAverage(allCloses, value).slice(windowStart, windowStart + visible.length),
  }));
  const priceValues = [
    ...visible.flatMap((row) => [row.low, row.high]),
    ...averages.flatMap((line) => line.values.filter((value): value is number => value !== null)),
  ];
  const priceMin = Math.min(...priceValues);
  const priceMax = Math.max(...priceValues);
  const pricePad = Math.max((priceMax - priceMin) * 0.08, priceMax * 0.005, 0.01);
  const priceLow = priceMin - pricePad;
  const priceHigh = priceMax + pricePad;
  const priceY = (value: number) =>
    priceBottom - ((value - priceLow) / Math.max(priceHigh - priceLow, 1e-9)) * (priceBottom - priceTop);

  const rawValues = visible
    .map((row) => row.factor_value)
    .filter((value): value is number => Number.isFinite(Number(value)));
  const rawMin = rawValues.length ? Math.min(...rawValues) : 0;
  const rawMax = rawValues.length ? Math.max(...rawValues) : 1;
  const rawPad = Math.max((rawMax - rawMin) * 0.12, Math.abs(rawMax || 1) * 0.03, 1e-6);
  const rawLow = rawMin - rawPad;
  const rawHigh = rawMax + rawPad;
  const rawY = (value: number) =>
    factorBottom - ((value - rawLow) / Math.max(rawHigh - rawLow, 1e-9)) * (factorBottom - factorTop);
  const scoreY = (value: number) =>
    factorBottom - (Math.max(0, Math.min(100, value)) / 100) * (factorBottom - factorTop);
  const rawPoints = visible
    .map((row, index) =>
      Number.isFinite(Number(row.factor_value))
        ? `${x(index)},${rawY(Number(row.factor_value))}`
        : "",
    )
    .filter(Boolean)
    .join(" ");
  const scorePoints = visible
    .map((row, index) =>
      Number.isFinite(Number(row.factor_score))
        ? `${x(index)},${scoreY(Number(row.factor_score))}`
        : "",
    )
    .filter(Boolean)
    .join(" ");
  const activeIndex = hoverIndex ?? (visibleEventIndex >= 0 ? visibleEventIndex : visible.length - 1);
  const active = visible[Math.max(0, Math.min(visible.length - 1, activeIndex))];
  const previous = visible[Math.max(0, activeIndex - 1)];
  const activeChange = previous?.close
    ? (active.close / previous.close - 1) * 100
    : 0;
  const eventX = visibleEventIndex >= 0 ? x(visibleEventIndex) : null;
  const eventRow = visibleEventIndex >= 0 ? visible[visibleEventIndex] : null;
  const calloutRight = eventX !== null && eventX < width * 0.62;
  const calloutX = eventX === null
    ? left
    : calloutRight
      ? Math.min(width - 310, eventX + 20)
      : Math.max(left + 4, eventX - 294);
  const maColors = ["#e8c858", "#62a9ed", "#b979dc"];
  const tickIndexes = [0, Math.floor((visible.length - 1) / 2), visible.length - 1];
  const eventScore = Number(eventRow?.factor_score);
  const preferredText = Number.isFinite(eventScore)
    ? eventScore >= 80
      ? `案例区间偏好分 ${eventScore.toFixed(0)}，属于较强的一组`
      : `案例区间偏好分 ${eventScore.toFixed(0)}，仅作对照，不代表必涨`
    : "该点展示原始指标值，不把它直接当作买入指令";

  return (
    <div className="qflow-annotated-example">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={`${factorName}的K线与指标同轴教学图`}
        onMouseLeave={() => setHoverIndex(null)}
      >
        <defs>
          <marker id="factor-callout-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
            <path d="M0,0 L8,4 L0,8 Z" fill="#f0a443" />
          </marker>
        </defs>
        <rect x="0" y="0" width={width} height={height} rx="14" className="chart-bg" />
        <text x={left} y="24" className="panel-title">价格 K 线（与下方指标共用同一日期轴）</text>
        <g className="ma-legend">
          {averages.map((line, index) => (
            <g key={line.period} transform={`translate(${left + 305 + index * 116},17)`}>
              <line x1="0" x2="18" y1="0" y2="0" stroke={maColors[index]} strokeWidth="3" />
              <text x="24" y="4">MA{line.period}</text>
            </g>
          ))}
        </g>
        {[0, 0.5, 1].map((ratio) => {
          const yValue = priceTop + ratio * (priceBottom - priceTop);
          const price = priceHigh - ratio * (priceHigh - priceLow);
          return <g key={ratio}><line x1={left} x2={width-right} y1={yValue} y2={yValue} className="grid-line"/><text x={6} y={yValue+4} className="axis-label">{price.toFixed(price < 10 ? 3 : 2)}</text></g>;
        })}
        {visible.map((row, index) => {
          const up = row.close >= row.open;
          const color = up ? "#ed5b61" : "#27ae7b";
          const bodyTop = priceY(Math.max(row.open, row.close));
          const bodyHeight = Math.max(1.4, Math.abs(priceY(row.open) - priceY(row.close)));
          return <g key={row.date}>
            <line x1={x(index)} x2={x(index)} y1={priceY(row.high)} y2={priceY(row.low)} stroke={color} strokeWidth="1.4"/>
            <rect x={x(index)-candleWidth/2} y={bodyTop} width={candleWidth} height={bodyHeight} fill={up ? color : "#0c1818"} stroke={color} strokeWidth="1.2"/>
            <rect className="hover-band" x={x(index)-plotWidth/visible.length/2} y={priceTop} width={plotWidth/visible.length} height={factorBottom-priceTop} onMouseEnter={() => setHoverIndex(index)}/>
          </g>;
        })}
        {averages.map((line, lineIndex) => (
          <polyline
            key={line.period}
            points={line.values.map((value, index) => value === null ? "" : `${x(index)},${priceY(value)}`).filter(Boolean).join(" ")}
            fill="none"
            stroke={maColors[lineIndex]}
            strokeWidth="2"
            strokeLinejoin="round"
          />
        ))}
        <rect x={left} y={factorTop-25} width={plotWidth} height={factorBottom-factorTop+25} rx="8" className="factor-pane" />
        <text x={left+10} y={factorTop-8} className="factor-title">② {factorName} 原始指标</text>
        <text x={left+300} y={factorTop-8} className="score-title">③ 案例区间偏好分（橙色虚线，右轴0—100）</text>
        {[0, 0.5, 1].map((ratio) => {
          const yValue = factorTop + ratio * (factorBottom-factorTop);
          const value = rawHigh - ratio * (rawHigh-rawLow);
          return <g key={ratio}><line x1={left} x2={width-right} y1={yValue} y2={yValue} className="factor-grid"/><text x={6} y={yValue+4} className="axis-label">{factorNumber(value, Math.abs(value)<10?3:2)}</text><text x={width-right+9} y={yValue+4} className="axis-label score-axis">{Math.round(100*(1-ratio))}</text></g>;
        })}
        <polyline points={rawPoints} fill="none" className="raw-factor-line" />
        <polyline points={scorePoints} fill="none" className="score-factor-line" />
        {eventX !== null && eventRow && (
          <g className="event-annotation">
            <line x1={eventX} x2={eventX} y1={priceTop} y2={factorBottom} className="event-guide" />
            <circle cx={eventX} cy={priceY(eventRow.close)} r="7" className="event-price-dot" />
            <circle cx={eventX} cy={rawY(Number(eventRow.factor_value || 0))} r="6" className="event-factor-dot" />
            <path d={`M${calloutRight ? calloutX : calloutX+282},92 L${eventX},${Math.max(priceTop+20,priceY(eventRow.high)-8)}`} className="callout-arrow" markerEnd="url(#factor-callout-arrow)" />
            <rect x={calloutX} y="53" width="282" height="66" rx="8" className="callout-box" />
            <text x={calloutX+12} y="74" className="callout-title">① 关键日期：{eventRow.date}</text>
            <text x={calloutX+12} y="94" className="callout-value">收盘 {eventRow.close.toFixed(eventRow.close<10?3:2)} · 指标 {factorNumber(eventRow.factor_value)}</text>
            <text x={calloutX+12} y="111" className="callout-hint">同一竖线向下对应指标点和偏好分</text>
          </g>
        )}
        {hoverIndex !== null && active && (
          <line x1={x(activeIndex)} x2={x(activeIndex)} y1={priceTop} y2={factorBottom} className="hover-guide" />
        )}
        {tickIndexes.map((index) => (
          <text key={index} x={x(index)} y="483" textAnchor="middle" className="date-label">{visible[index]?.date.slice(5)}</text>
        ))}
      </svg>
      <div className="qflow-factor-linked-readout">
        <b>{hoverIndex === null ? "关键点对应值" : "鼠标所在日期"}</b>
        <span>{active.date}</span>
        <span>收盘 <strong>{active.close.toFixed(active.close < 10 ? 3 : 2)}</strong></span>
        <span>当日涨跌 <strong className={activeChange >= 0 ? "up" : "down"}>{activeChange >= 0 ? "+" : ""}{activeChange.toFixed(2)}%</strong></span>
        <span>{factorName} <strong>{factorNumber(active.factor_value)}</strong></span>
        <span>偏好分 <strong>{factorNumber(active.factor_score, 0)}</strong></span>
      </div>
      {eventRow && (
        <div className="qflow-factor-point-explanation">
          <article><i>1</i><span><b>先看 K 线</b><p>{eventRow.date} 收盘价是 {eventRow.close.toFixed(eventRow.close < 10 ? 3 : 2)}，图中橙色圆点就是被选中的关键 K 线。</p></span></article>
          <article><i>2</i><span><b>再沿竖线往下看指标</b><p>同一天的 {factorName} 原值为 {factorNumber(eventRow.factor_value)}；蓝线的圆点与上方 K 线严格对应。</p></span></article>
          <article><i>3</i><span><b>最后看它在区间里的位置</b><p>{preferredText}。偏好分只方便讲解，不替代全市场横截面排名。</p></span></article>
        </div>
      )}
    </div>
  );
}

function FactorTeachingPanel({ block, meta }: { block: BlockSpec; meta: BlockMeta }) {
  const [code, setCode] = useState("600519");
  const [result, setResult] = useState<FactorExampleResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const loadExample = useCallback(async (requestedCode?: string) => {
    const nextCode = String(requestedCode || code).replace(/\D/g, "").slice(0, 6);
    if (nextCode.length !== 6) { setError("请输入 6 位股票代码"); return; }
    setLoading(true); setError("");
    try {
      const payload = await api<FactorExampleResult>("/factor-example", {
        method: "POST",
        body: JSON.stringify({ factor_type: block.type, code: nextCode, params: block.params, limit: 120 }),
      });
      setResult(payload);
    } catch (reason: unknown) { setError(errorMessage(reason)); setResult(null); }
    finally { setLoading(false); }
  }, [block.type, block.params, code]);
  useEffect(() => {
    const timer = window.setTimeout(() => { void loadExample("600519"); }, 0);
    return () => window.clearTimeout(timer);
  }, [block.type, loadExample]);
  const teaching = result?.teaching || meta;
  const event = result?.example;
  return <section className="qflow-factor-teaching">
    <header><div><Lightbulb/><span><b>这个因子怎么用</b><small>解释 + 本地真实 K 线案例</small></span></div></header>
    <div className="qflow-factor-lessons">
      <article><b>通俗理解</b><p>{teaching.plain_explanation || meta.description}</p></article>
      <article><b>怎么看数值</b><p>{teaching.how_to_read || "结合方向、标准化方式和权重理解，不把单一数值直接当作买卖指令。"}</p></article>
      <article><b>适合做什么</b><p>{teaching.best_for || "用于候选股横截面比较和多因子排序。"}</p></article>
      <article className="warning"><b>容易踩的坑</b><p>{teaching.pitfall || "历史有效不代表未来有效，必须配合样本外检验。"}</p></article>
    </div>
    <div className="qflow-factor-example-controls">
      <label><span>案例股票</span><input value={code} inputMode="numeric" maxLength={6} onChange={event => setCode(event.target.value.replace(/\D/g, "").slice(0, 6))}/></label>
      <button onClick={() => void loadExample()} disabled={loading}>{loading ? <LoaderCircle className="spin"/> : <RefreshCw/>}{loading ? "计算中" : "按当前参数刷新"}</button>
    </div>
    {error && <div className="qflow-factor-example-error"><CircleAlert/>{error}</div>}
    {result && <>
      <div className="qflow-factor-example-head"><span><b>{result.name}</b><small>{result.code} · 本地 DuckDB · 案例区间 {result.chart_from || result.candles[0]?.date} 至 {result.chart_to || result.candles.at(-1)?.date} · 数据截至 {result.as_of}</small></span><em>{result.factor_name}</em></div>
      <FactorAnnotatedExample rows={result.candles} event={event} factorName={result.factor_name} block={block}/>
      <p className="qflow-factor-example-caption">{teaching.example_caption}</p>
      <div className="qflow-factor-example-stats">
        <div><span>最新原值</span><b>{Number.isFinite(Number(result.statistics.latest)) ? Number(result.statistics.latest).toFixed(4) : "--"}</b></div>
        <div><span>区间中位</span><b>{Number.isFinite(Number(result.statistics.median)) ? Number(result.statistics.median).toFixed(4) : "--"}</b></div>
        <div><span>典型日期</span><b>{event?.date || "--"}</b></div>
        <div><span>其后5日复盘</span><b className={Number(event?.forward_5_return || 0) >= 0 ? "up" : "down"}>{event ? formatRatio(event.forward_5_return) : "--"}</b></div>
      </div>
      {event && <p className="qflow-factor-example-note">{event.note}</p>}
    </>}
  </section>;
}

const EQUITY_CHART_SETTINGS = {
  maShort: 5,
  maMid: 10,
  maLong: 20,
};
function EquityKline({ rows }: { rows: JsonRecord[] }) {
  const data = rows.map((row, index) => {
    const close = Number(row.equity),
      open = index ? Number(rows[index - 1].equity) : close;
    return {
      date: String(row.date).slice(0, 10),
      open,
      close,
      high: Math.max(open, close),
      low: Math.min(open, close),
      volume: 0,
      amount: 0,
    };
  });
  if (!data.length)
    return <div className="qflow-empty-result">暂无净值数据</div>;
  return (
    <MatureKlineChart
      data={data}
      settings={EQUITY_CHART_SETTINGS}
      mainIndicator="MA"
      height={280}
    />
  );
}

type ResultTab = "performance" | "factors" | "ranking" | "trades";

function FactorResearch({ result }: { result: BacktestResult }) {
  const groupsByFactor = (result.factor_groups || []).reduce<
    Record<string, JsonRecord[]>
  >((groups, row) => {
    const key = String(row.block_id || row.name);
    groups[key] = [...(groups[key] || []), row];
    return groups;
  }, {});
  return (
    <div className="qflow-research-stack">
      <section className="qflow-result-section">
        <h3>因子有效性</h3>
        <div className="qflow-table-wrap">
          <table className="qflow-data-table">
            <thead>
              <tr>
                <th>因子</th>
                <th>权重</th>
                <th>Rank IC 20日</th>
                <th>ICIR</th>
                <th>正向比例</th>
                <th>五组多空差</th>
                <th>样本</th>
              </tr>
            </thead>
            <tbody>
              {(result.factor_analysis || []).map((row) => (
                <tr key={String(row.block_id)}>
                  <td>
                    <b>{row.name}</b>
                    <small>
                      {row.direction === "lower" ? "越小越好" : "越大越好"}
                    </small>
                  </td>
                  <td>{formatRatio(row.weight)}</td>
                  <td className={Number(row.rank_ic_20) >= 0 ? "up" : "down"}>
                    {Number.isFinite(Number(row.rank_ic_20))
                      ? Number(row.rank_ic_20).toFixed(4)
                      : "--"}
                  </td>
                  <td>
                    {Number.isFinite(Number(row.icir_20))
                      ? Number(row.icir_20).toFixed(3)
                      : "--"}
                  </td>
                  <td>{formatRatio(row.positive_rate_20)}</td>
                  <td className={Number(row.spread_20) >= 0 ? "up" : "down"}>
                    {formatRatio(row.spread_20)}
                  </td>
                  <td>
                    {Number(row.observations || 0).toLocaleString("zh-CN")}
                    <small>{row.dates || 0}日</small>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <section className="qflow-result-section">
        <h3>五分组未来收益</h3>
        <div className="qflow-factor-groups">
          {Object.entries(groupsByFactor).map(([blockId, rows]) => {
            const ordered = [...rows].sort(
              (a, b) => Number(a.bucket) - Number(b.bucket),
            );
            const scale = Math.max(
              0.000001,
              ...ordered.map((row) =>
                Math.abs(Number(row.forward_return_20) || 0),
              ),
            );
            return (
              <article key={blockId}>
                <b>{ordered[0]?.name || blockId}</b>
                {ordered.map((row) => {
                  const value = Number(row.forward_return_20);
                  return (
                    <div key={String(row.bucket)}>
                      <span>G{row.bucket}</span>
                      <i>
                        <em
                          className={value >= 0 ? "up" : "down"}
                          style={{
                            width: `${Math.max(2, (Math.abs(value) / scale) * 100)}%`,
                          }}
                        />
                      </i>
                      <strong className={value >= 0 ? "up" : "down"}>
                        {formatRatio(value)}
                      </strong>
                    </div>
                  );
                })}
              </article>
            );
          })}
        </div>
      </section>
      <section className="qflow-result-section">
        <h3>因子相关性</h3>
        <div className="qflow-correlation-list">
          {(result.factor_correlations || []).length ? (
            result.factor_correlations.map((row, index) => (
              <div key={`${row.block_a}-${row.block_b}-${index}`}>
                <span>{row.name_a}</span>
                <i>↔</i>
                <span>{row.name_b}</span>
                <b
                  className={
                    Math.abs(Number(row.correlation)) >= 0.7 ? "warn" : ""
                  }
                >
                  {Number.isFinite(Number(row.correlation))
                    ? Number(row.correlation).toFixed(3)
                    : "--"}
                </b>
              </div>
            ))
          ) : (
            <p>只有一个因子，暂无成对相关性。</p>
          )}
        </div>
      </section>
    </div>
  );
}

function zAuditFor(row: Record<string, unknown>) {
  const signal = row.z_signal ?? row.z_entry_signal ?? row.z_signal_source;
  const score = row.z_resonance_score ?? row.z_score;
  const line = row.z_line_state ?? row.dual_line_state;
  const brick = row.z_brick_color ?? row.brick_color;
  const exit = row.z_exit_reason;
  const rawComponents = row.z_components;
  const components = Array.isArray(rawComponents)
    ? rawComponents.map(String)
    : rawComponents && typeof rawComponents === "object"
      ? Object.entries(rawComponents)
          .filter(([, hit]) => Boolean(hit))
          .map(([name]) => name)
      : [];
  const hasAudit = [signal, score, line, brick, exit].some(
    (value) => value !== undefined && value !== null && value !== "",
  ) || components.length > 0;
  return {
    hasAudit,
    signal: signal == null ? "" : String(signal),
    score: Number.isFinite(Number(score)) ? Number(score) : null,
    line: line == null ? "" : String(line),
    brick: brick == null ? "" : String(brick),
    exit: exit == null ? "" : String(exit),
    components,
  };
}

function ZAuditCell({ row }: { row: Record<string, unknown> }) {
  const audit = zAuditFor(row);
  if (!audit.hasAudit) return <>--</>;
  return (
    <span className="qflow-z-audit-cell">
      <b>
        {audit.signal || "Z 共振"}
        {audit.score !== null ? ` · ${audit.score.toFixed(2)}分` : ""}
      </b>
      <small>
        {[audit.line, audit.brick, ...audit.components.slice(0, 3), audit.exit]
          .filter(Boolean)
          .join(" · ") || "规则轨迹已记录"}
      </small>
    </span>
  );
}

function RankingTable({
  rows,
  totalRows = rows.length,
}: {
  rows: JsonRecord[];
  totalRows?: number;
}) {
  const dates = useMemo(
    () =>
      [...new Set(rows.map(rankDate).filter(Boolean))].sort(),
    [rows],
  );
  const [selectedDate, setSelectedDate] = useState(() => dates.at(-1) || "");
  const [showAll, setShowAll] = useState(false);
  const effectiveDate = dates.includes(selectedDate)
    ? selectedDate
    : dates.at(-1) || "";
  const dateIndex = dates.indexOf(effectiveDate);
  const visibleRows = showAll
    ? rows
    : rows.filter((row) => rankDate(row) === effectiveDate);
  const hasZAudit = rows.some((row) => zAuditFor(row).hasAudit);
  const chooseDate = (candidate: string) => {
    if (!dates.length) return;
    if (dates.includes(candidate)) {
      setSelectedDate(candidate);
      return;
    }
    // A browser date input can contain a weekend/holiday. Snap to the next
    // available trading date, then to the latest date at the end of the range.
    setSelectedDate(
      dates.find((date) => date >= candidate) || dates.at(-1) || "",
    );
  };
  return (
    <section className="qflow-result-section qflow-fill-section">
      <div className="qflow-ranking-head">
        <div>
          <h3>每日候选排名</h3>
          <span>
            {dates.length} 个交易日 · 后端完整 {totalRows.toLocaleString("zh-CN")} 行
          </span>
        </div>
        <div className="qflow-ranking-date">
          <button
            type="button"
            title="上一个交易日"
            aria-label="上一个交易日"
            disabled={showAll || dateIndex <= 0}
            onClick={() => setSelectedDate(dates[dateIndex - 1])}
          >
            <ChevronLeft />
          </button>
          <input
            type="date"
            min={dates[0]}
            max={dates.at(-1)}
            value={effectiveDate}
            disabled={showAll || !dates.length}
            onChange={(event) => chooseDate(event.target.value)}
            aria-label="选择排名交易日"
          />
          <button
            type="button"
            title="下一个交易日"
            aria-label="下一个交易日"
            disabled={showAll || dateIndex < 0 || dateIndex >= dates.length - 1}
            onClick={() => setSelectedDate(dates[dateIndex + 1])}
          >
            <ChevronRight />
          </button>
          <label>
            <input
              type="checkbox"
              checked={showAll}
              onChange={(event) => setShowAll(event.target.checked)}
            />
            全部日期
          </label>
        </div>
      </div>
      <div className="qflow-ranking-summary">
        {showAll
          ? `全部 ${visibleRows.length.toLocaleString("zh-CN")} 行 · 后端完整 ${totalRows.toLocaleString("zh-CN")} 行`
          : `${effectiveDate || "--"} · ${visibleRows.length.toLocaleString("zh-CN")} 只候选 · 后端完整 ${totalRows.toLocaleString("zh-CN")} 行`}
      </div>
      <div className="qflow-table-wrap">
        <table className="qflow-data-table">
          <thead>
            <tr>
              <th>信号日</th>
              <th>排名</th>
              <th>股票</th>
              <th>收盘价</th>
              <th>最终分</th>
              {hasZAudit && <th>Z 规则轨迹</th>}
            </tr>
          </thead>
          <tbody>
            {visibleRows.length ? (
              visibleRows.map((row, index) => (
                <tr key={`${rankDate(row)}-${row.instrument_id || row.code}-${index}`}>
                  <td>{rankDate(row) || "--"}</td>
                  <td>#{row.candidate_rank ?? "--"}</td>
                  <td>
                    <b>{row.name || row.code || "--"}</b>
                    <small>{row.code || row.ticker || "--"}</small>
                  </td>
                  <td>
                    {Number.isFinite(Number(row.signal_close))
                      ? Number(row.signal_close).toFixed(2)
                      : "--"}
                  </td>
                  <td>
                    {Number.isFinite(Number(row.final_score ?? row.score))
                      ? Number(row.final_score ?? row.score).toFixed(3)
                      : "--"}
                  </td>
                  {hasZAudit && <td><ZAuditCell row={row} /></td>}
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan={hasZAudit ? 6 : 5} className="qflow-table-empty">
                  {dates.length ? "该交易日没有排名记录" : "后端没有返回排名明细"}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function StockPnlTable({ result }: { result: BacktestResult }) {
  const rows = result.stock_pnl || [];
  return (
    <section className="qflow-result-section qflow-stock-pnl">
      <h3>逐股票累计盈亏 · {rows.length}只</h3>
      <div className="qflow-table-wrap">
        <table className="qflow-data-table">
          <thead><tr><th>股票</th><th>平仓次数</th><th>盈利 / 亏损</th><th>累计盈亏</th><th>累计收益率</th></tr></thead>
          <tbody>{rows.map((row, index) => <tr key={`${row.code}-${index}`}>
            <td><b>{row.name || row.ticker || row.code}</b><small>{row.ticker || row.code}</small></td>
            <td>{Number(row.trades || 0)}</td>
            <td><i className="up">{Number(row.wins || 0)}</i> / <i className="down">{Number(row.losses || 0)}</i></td>
            <td className={Number(row.pnl) >= 0 ? "up" : "down"}>{Number(row.pnl || 0).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}</td>
            <td className={Number(row.realized_return) >= 0 ? "up" : "down"}>{formatRatio(row.realized_return)}</td>
          </tr>)}</tbody>
        </table>
      </div>
    </section>
  );
}

function TradeTables({ result }: { result: BacktestResult }) {
  return (
    <div className="qflow-research-stack">
      <StockPnlTable result={result} />
      <section className="qflow-result-section">
        <h3>全部成交记录 · {result.trades?.length || 0}笔</h3>
        <div className="qflow-table-wrap">
          <table className="qflow-data-table">
            <thead>
              <tr>
                <th>日期</th>
                <th>股票</th>
                <th>方向</th>
                <th>成交价</th>
                <th>数量</th>
                <th>金额</th>
                <th>原因</th>
              </tr>
            </thead>
            <tbody>
              {(result.trades || []).map((row, index) => (
                <tr key={`${row.date}-${row.code}-${index}`}>
                  <td>{String(row.date).slice(0, 10)}</td>
                  <td>
                    <b>{row.name || row.symbol || row.code}</b>
                    <small>{row.ticker || row.code}</small>
                  </td>
                  <td className={row.side === "买入" ? "up" : "down"}>
                    {row.side}
                  </td>
                  <td>{Number(row.price || row.execution_price).toFixed(2)}</td>
                  <td>
                    {Number(row.shares || row.size || 0).toLocaleString(
                      "zh-CN",
                    )}
                  </td>
                  <td>
                    {Number(row.amount || 0).toLocaleString("zh-CN", {
                      maximumFractionDigits: 0,
                    })}
                  </td>
                  <td>
                    <b>{row.reason || row.exit_reason || row.z_exit_reason || "--"}</b>
                    {zAuditFor(row).hasAudit && <ZAuditCell row={row} />}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <section className="qflow-result-section">
        <h3>已平仓单笔盈亏 · {result.closed_trades?.length || 0}笔</h3>
        <div className="qflow-table-wrap">
          <table className="qflow-data-table">
            <thead>
              <tr>
                <th>股票</th>
                <th>买入日</th>
                <th>卖出日</th>
                <th>数量</th>
                <th>单笔盈亏</th>
                <th>收益率</th>
              </tr>
            </thead>
            <tbody>
              {(result.closed_trades || []).map((row, index) => {
                const rate =
                  row.return_rate ?? row.realized_return ?? row.pnl_rate;
                return (
                  <tr key={`${row.entry_date}-${row.code}-${index}`}>
                    <td>
                      <b>{row.name || row.symbol || row.code}</b>
                      <small>{row.ticker || row.code}</small>
                    </td>
                    <td>{String(row.entry_date || "").slice(0, 10)}</td>
                    <td>{String(row.exit_date || "").slice(0, 10)}</td>
                    <td>{Number(row.shares || 0).toLocaleString("zh-CN")}</td>
                    <td className={Number(row.pnl) >= 0 ? "up" : "down"}>
                      {Number(row.pnl || 0).toLocaleString("zh-CN", {
                        maximumFractionDigits: 2,
                      })}
                    </td>
                    <td className={Number(rate) >= 0 ? "up" : "down"}>
                      {formatRatio(rate)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

function ResultDrawer({
  open,
  running,
  result,
  error,
  onClose,
}: {
  open: boolean;
  running: boolean;
  result: BacktestResult | null;
  error: string;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<ResultTab>("performance");
  const rankingRows = result ? rankingRowsFor(result) : [];
  const rankingTotal = result ? rankingTotalFor(result, rankingRows) : 0;
  return (
    <aside className={`qflow-result-drawer ${open ? "open" : ""}`}>
      <div className="qflow-result-head">
        <div>
          <CirclePlay />
          <span>
            <b>
              {running
                ? "回测正在运行"
                : result
                  ? `回测结果 ${result.run_id}`
                  : "回测结果"}
            </b>
            <small>全市场日线 · A股次日开盘模拟</small>
          </span>
        </div>
        <button onClick={onClose}>
          <X />
        </button>
      </div>
      {running ? (
        <div className="qflow-result-loading">
          <LoaderCircle className="spin" />
          <b>正在执行节点图</b>
          <span>筛选、因子研究、每日排名和A股撮合均在本机运行</span>
        </div>
      ) : result ? (
        <>
          <nav className="qflow-result-tabs">
            <button
              className={tab === "performance" ? "active" : ""}
              onClick={() => setTab("performance")}
            >
              策略表现
            </button>
            <button
              className={tab === "factors" ? "active" : ""}
              onClick={() => setTab("factors")}
            >
              因子研究 <em>{result.factor_analysis?.length || 0}</em>
            </button>
            <button
              className={tab === "ranking" ? "active" : ""}
              onClick={() => setTab("ranking")}
            >
              排名明细 <em>{rankingTotal.toLocaleString("zh-CN")}</em>
            </button>
            <button
              className={tab === "trades" ? "active" : ""}
              onClick={() => setTab("trades")}
            >
              交易记录 <em>{result.trades?.length || 0}</em>
            </button>
          </nav>
          <div className="qflow-result-body">
            {tab === "performance" && (
              <>
                <div className="qflow-metrics">
                  {[
                    ["total_return", "累计收益"],
                    ["cagr", "年化收益"],
                    ["max_drawdown", "最大回撤"],
                    ["sharpe", "夏普"],
                    ["win_rate", "胜率"],
                    ["final_equity", "期末权益"],
                  ].map(([key, label]) => (
                    <div key={key}>
                      <span>{label}</span>
                      <b>{formatMetric(key, result.metrics[key])}</b>
                    </div>
                  ))}
                </div>
                <EquityKline rows={result.equity || []} />
                <div className="qflow-result-grid">
                  <section>
                    <h3>数据沙漏</h3>
                    <div className="qflow-funnel">
                      {(result.stage_funnel || []).map((row, index) => (
                        <div key={index}>
                          <span>{row.name || row.stage || row.type}</span>
                          <b>{row.before_instruments ?? row.before ?? "--"}</b>
                          <i>→</i>
                          <strong>
                            {row.after_instruments ?? row.after ?? "--"}
                          </strong>
                        </div>
                      ))}
                    </div>
                  </section>
                  <section>
                    <h3>全部成交记录 · {result.trades?.length || 0}笔</h3>
                    <div
                      className="qflow-trades qflow-trades-scroll"
                      role="region"
                      aria-label="策略表现全部成交记录"
                    >
                      {[...(result.trades || [])]
                        .reverse()
                        .map((trade, index) => (
                          <div key={index}>
                            <span>
                              <b>{trade.symbol || trade.name || trade.code}</b>
                              <small>
                                {String(trade.date).slice(0, 10)} · {trade.side}
                              </small>
                            </span>
                            <strong>{Number(trade.price).toFixed(2)}</strong>
                            <em
                              className={
                                Number(trade.realized_return) >= 0
                                  ? "up"
                                  : "down"
                              }
                            >
                              {trade.realized_return == null
                                ? "--"
                                : formatRatio(trade.realized_return)}
                            </em>
                          </div>
                        ))}
                    </div>
                  </section>
                </div>
                <StockPnlTable result={result} />
              </>
            )}
            {tab === "factors" && <FactorResearch result={result} />}{" "}
            {tab === "ranking" && (
              <RankingTable
                key={result.run_id}
                rows={rankingRows}
                totalRows={rankingTotal}
              />
            )}{" "}
            {tab === "trades" && <TradeTables result={result} />}
          </div>
        </>
      ) : (
        <div className={`qflow-empty-result ${error ? "has-error" : ""}`}>
          {error ? <CircleAlert /> : <Database />}
          <b>{error ? "当前工作流无法回测" : "还没有成功运行过回测"}</b>
          <span>
            {error ||
              "请先搭建并连接完整工作流，再点击运行；成功后这里会显示净值、排名和交易记录。"}
          </span>
        </div>
      )}
    </aside>
  );
}

export default function QuantWorkflowStudio({ onContextChange }: { onContextChange?: (context: ResearchContext) => void }) {
  const [catalog, setCatalog] = useState<Record<string, BlockMeta>>({});
  const [pipeline, setPipeline] = useState<Pipeline | null>(null);
  const [blankTemplate, setBlankTemplate] = useState<Pipeline | null>(null);
  const [presets, setPresets] = useState<Record<string, Pipeline>>({});
  const [nodes, setNodes, onNodesChange] = useNodesState<QuantNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [flow, setFlow] = useState<ReactFlowInstance<QuantNode, Edge> | null>(
    null,
  );
  const [search, setSearch] = useState("");
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [saved, setSaved] = useState<SavedPipeline[]>([]);
  const [savedId, setSavedId] = useState("");
  const [versions, setVersions] = useState<SavedPipeline[]>([]);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [inspectorTab, setInspectorTab] = useState<
    "settings" | "versions" | "runs"
  >("settings");
  const [mobilePanel, setMobilePanel] = useState<
    "library" | "inspector" | null
  >(null);
  const [libraryVisible, setLibraryVisible] = useState(true);
  const [inspectorVisible, setInspectorVisible] = useState(true);
  const [inspectorWidth, setInspectorWidth] = useState(520);
  const [resizingInspector, setResizingInspector] = useState(false);
  const inspectorDragRef = useRef({ startX: 0, startWidth: 520 });
  const [running, setRunning] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [resultOpen, setResultOpen] = useState(false);
  const [savingTemplate, setSavingTemplate] = useState(false);
  const importRef = useRef<HTMLInputElement>(null);

  const compactLayout = () =>
    typeof window !== "undefined" &&
    window.matchMedia("(max-width: 760px)").matches;
  const togglePanel = (panel: "library" | "inspector") => {
    if (compactLayout()) {
      setMobilePanel((current) => (current === panel ? null : panel));
      return;
    }
    if (panel === "library") setLibraryVisible((current) => !current);
    else setInspectorVisible((current) => !current);
  };
  const showInspector = () => {
    if (compactLayout()) setMobilePanel("inspector");
    else setInspectorVisible(true);
  };
  const startInspectorResize = (event: React.PointerEvent<HTMLButtonElement>) => {
    event.preventDefault();
    inspectorDragRef.current = { startX: event.clientX, startWidth: inspectorWidth };
    setResizingInspector(true);
  };

  useEffect(() => {
    if (!resizingInspector) return;
    const move = (event: PointerEvent) => {
      const mobile = window.matchMedia("(max-width: 760px)").matches;
      const available = mobile
        ? window.innerWidth * 0.98
        : Math.max(360, window.innerWidth - (libraryVisible ? 300 : 20) - 240);
      const next = inspectorDragRef.current.startWidth + inspectorDragRef.current.startX - event.clientX;
      setInspectorWidth(Math.max(mobile ? 280 : 340, Math.min(available, next)));
    };
    const stop = () => setResizingInspector(false);
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop, { once: true });
    document.body.classList.add("qflow-resizing-inspector");
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      document.body.classList.remove("qflow-resizing-inspector");
    };
  }, [libraryVisible, resizingInspector]);

  useEffect(() => {
    api<{
      catalog: Record<string, BlockMeta>;
      default_pipeline: Pipeline;
      blank_pipeline?: Pipeline;
      presets?: Record<string, Pipeline>;
      saved: SavedPipeline[];
    }>("/catalog")
      .then((payload) => {
        const blank = payload.blank_pipeline || payload.default_pipeline;
        const graph = pipelineGraph(blank, payload.catalog);
        setCatalog(payload.catalog);
        setBlankTemplate(blank);
        setPresets(payload.presets || {});
        setPipeline(blank);
        setNodes(graph.nodes);
        setEdges(graph.edges);
        setSaved(payload.saved || []);
      })
      .catch((reason: unknown) => setError(errorMessage(reason)));
  }, [setEdges, setNodes]);
  const selectedNode = nodes.find((node) => node.id === selectedNodeId) || null;
  const selectedNodeName = selectedNode?.data.block.name || "";

  useEffect(() => {
    const nodeSummary = nodes.length ? `${nodes.length} 个节点，${edges.length} 条连线` : "空白画布，尚未添加节点";
    const selectedSummary = selectedNodeName ? `当前选中：${selectedNodeName}` : "当前未选中节点";
    onContextChange?.({
      surface: "studio",
      title: `策略画布 · ${pipeline?.name || "未命名工作流"}`,
      summary: running ? "正在运行工作流回测，请等待结果审计。" : result ? "正在查看最近一次回测结果并准备继续调整流程。" : "正在把股票池、筛选、因子、组合和执行节点连接成可回测流程。",
      details: [nodeSummary, selectedSummary, error ? "存在校验错误" : notice || "等待下一步操作"],
      suggestions: ["告诉我这条工作流现在缺哪一个节点", "检查当前工作流是否有未来函数风险", "把当前流程改成五仓位、次日执行的规则"],
    });
  }, [edges.length, error, nodes.length, notice, onContextChange, pipeline?.name, result, running, selectedNodeName]);

  const serialize = useCallback((): Pipeline | null => {
    if (!pipeline) return null;
    return {
      ...pipeline,
      blocks: nodes.map((node) => normalizeBlock(node.data.block)),
      workflow: {
        nodes: nodes.map((node) => ({ id: node.id, position: node.position })),
        edges: edges.map((edge) => ({
          id: edge.id,
          source: edge.source,
          target: edge.target,
          label: String(edge.label || "数据流"),
        })),
      },
    };
  }, [edges, nodes, pipeline]);

  const replacePipeline = useCallback(
    (next: Pipeline, message = `已载入：${next.name}`) => {
      const graph = pipelineGraph(next, catalog);
      setPipeline(next);
      setNodes(graph.nodes);
      setEdges(graph.edges);
      setSelectedNodeId(null);
      setResult(null);
      setNotice(message);
    },
    [catalog, setEdges, setNodes],
  );
  const refreshSaved = useCallback(async () => {
    const payload = await api<{ pipelines: SavedPipeline[] }>("/pipelines");
    setSaved(payload.pipelines || []);
  }, []);
  const refreshVersions = useCallback(async (pipelineId: string) => {
    if (!pipelineId) {
      setVersions([]);
      return;
    }
    const payload = await api<{ versions: SavedPipeline[] }>(
      `/pipelines/${pipelineId}/versions`,
    );
    setVersions(payload.versions || []);
  }, []);
  const refreshRuns = useCallback(async (pipelineId = "") => {
    const query = pipelineId
      ? `?pipeline_id=${encodeURIComponent(pipelineId)}&limit=50`
      : "?limit=50";
    const payload = await api<{ runs: RunSummary[] }>(`/runs${query}`);
    setRuns(payload.runs || []);
  }, []);
  const updateSelectedBlock = (update: (block: BlockSpec) => BlockSpec) =>
    setNodes((current) =>
      current.map((node) =>
        node.id === selectedNodeId
          ? { ...node, data: { ...node.data, block: update(node.data.block) } }
          : node,
      ),
    );
  const isValidConnection = (connection: Edge | Connection) => {
    const source = nodes.find((node) => node.id === connection.source),
      target = nodes.find((node) => node.id === connection.target);
    return Boolean(
      source &&
        target &&
        source.id !== target.id &&
        stageIndex(source.data.block.category) <=
          stageIndex(target.data.block.category),
    );
  };
  const onConnect = useCallback(
    (connection: Connection) =>
      setEdges((current) =>
        addEdge(
          {
            ...connection,
            id: `edge-${connection.source}-${connection.target}-${Date.now()}`,
            type: "smoothstep",
            animated: true,
            markerEnd: { type: MarkerType.ArrowClosed, color: "#72c7ab" },
            style: { stroke: "#72c7ab", strokeWidth: 2 },
            label: "数据流",
          },
          current,
        ),
      ),
    [setEdges],
  );
  const addBlock = (type: string, position?: { x: number; y: number }) => {
    const meta = catalog[type];
    if (!meta) return;
    if (meta.available === false || meta.disabled_reason) {
      setNotice(`${meta.name} 暂不可用：${meta.disabled_reason}`);
      return;
    }
    const params = structuredClone(meta.params || {});
    const executableType = meta.base_type || type;
    if (meta.category === "universe") {
      const currentUniverse = nodes.find(
        (node) => node.data.block.category === "universe",
      );
      if (currentUniverse) {
        const block: BlockSpec = {
          id: currentUniverse.id,
          type: executableType,
          category: "universe",
          name: meta.name,
          enabled: true,
          combine: "AND",
          params,
        };
        setNodes((current) =>
          current.map((node) =>
            node.id === currentUniverse.id
              ? { ...node, data: { block, meta } }
              : node,
          ),
        );
        setSelectedNodeId(currentUniverse.id);
        setNotice(`数据源已切换为 ${meta.name}`);
        return;
      }
    }
    const block: BlockSpec = {
      id: newId(type),
      type: executableType,
      category: meta.category,
      name: isCustomFormulaBlock(executableType)
        ? String(params.name || meta.name)
        : meta.name,
      enabled: true,
      combine: "AND",
      params,
    };
    const row = nodes.filter(
      (node) => node.data.block.category === meta.category,
    ).length;
    const node: QuantNode = {
      id: block.id,
      type: "quant",
      position: position || {
        x: 100 + stageIndex(meta.category) * 280,
        y: 80 + row * 170,
      },
      data: { block, meta },
    };
    const nextNodes = [...nodes, node];
    const ordered = [...nextNodes].sort(
      (a, b) =>
        stageIndex(a.data.block.category) - stageIndex(b.data.block.category) ||
        a.position.y - b.position.y,
    );
    setNodes(nextNodes);
    setEdges(ordered.slice(0, -1).map((current, index) => ({
      id: `edge-${current.id}-${ordered[index + 1].id}`,
      source: current.id,
      target: ordered[index + 1].id,
      type: "smoothstep",
      animated: true,
      markerEnd: { type: MarkerType.ArrowClosed, color: "#72c7ab" },
      style: { stroke: "#72c7ab", strokeWidth: 2 },
      label: "数据流",
    })));
    setSelectedNodeId(block.id);
    setNotice(`已加入 ${meta.name}，并按阶段自动接通数据流`);
  };
  const onDrop = (event: React.DragEvent) => {
    event.preventDefault();
    const type = event.dataTransfer.getData("application/quantweave-node");
    if (type && flow)
      addBlock(
        type,
        flow.screenToFlowPosition({ x: event.clientX, y: event.clientY }),
      );
  };
  const autoLayout = () => {
    const rows: Record<string, number> = {};
    setNodes((current) =>
      current.map((node) => {
        const stage = node.data.block.category,
          row = rows[stage] || 0;
        rows[stage] = row + 1;
        return {
          ...node,
          position: { x: 90 + stageIndex(stage) * 280, y: 80 + row * 170 },
        };
      }),
    );
    setTimeout(() => flow?.fitView({ padding: 0.15, duration: 500 }), 20);
  };
  const standardWire = () => {
    const ordered = [...nodes].sort(
      (a, b) =>
        stageIndex(a.data.block.category) - stageIndex(b.data.block.category) ||
        a.position.y - b.position.y,
    );
    setEdges(
      ordered.slice(0, -1).map((node, index) => ({
        id: `edge-${node.id}-${ordered[index + 1].id}`,
        source: node.id,
        target: ordered[index + 1].id,
        type: "smoothstep",
        animated: true,
        markerEnd: { type: MarkerType.ArrowClosed, color: "#72c7ab" },
        style: { stroke: "#72c7ab", strokeWidth: 2 },
        label: "数据流",
      })),
    );
  };
  const validate = async () => {
    const current = serialize();
    if (!current) return;
    setError("");
    try {
      await api("/validate", {
        method: "POST",
        body: JSON.stringify({ pipeline: current }),
      });
      setNotice("工作流校验通过，可以运行全市场回测");
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    }
  };
  const run = async () => {
    const current = serialize();
    if (!current) return;
    setRunning(true);
    setError("");
    setResultOpen(true);
    try {
      const payload = await api<BacktestResult>("/backtests", {
        method: "POST",
        body: JSON.stringify({ pipeline: current }),
      });
      setResult(payload);
      setNotice(`回测完成：${payload.run_id}`);
      await refreshRuns(current.id);
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    } finally {
      setRunning(false);
    }
  };
  const save = async () => {
    const current = serialize();
    if (!current) return;
    setError("");
    try {
      const payload = await api<SavedPipeline>("/pipelines", {
        method: "POST",
        body: JSON.stringify({ pipeline: current, name: current.name }),
      });
      setPipeline({ ...current, revision: payload.revision });
      setSavedId(current.id);
      setNotice(`已保存：${payload.name} · 版本 ${payload.revision}`);
      await Promise.all([refreshSaved(), refreshVersions(current.id)]);
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    }
  };
  const saveSelectedAsTemplate = async () => {
    if (!selectedNode || !isCustomFormulaBlock(selectedNode.data.block.type))
      return;
    setSavingTemplate(true);
    setError("");
    try {
      const payload = await api<{
        catalog: Record<string, BlockMeta>;
        meta: BlockMeta;
        template: JsonRecord;
      }>("/custom-blocks", {
        method: "POST",
        body: JSON.stringify({
          block: selectedNode.data.block,
          template_id: selectedNode.data.meta.template_id || null,
          description: `固定模块 · ${selectedNode.data.block.params.formula || selectedNode.data.meta.description}`,
        }),
      });
      setCatalog(payload.catalog);
      setNodes((current) =>
        current.map((node) =>
          node.id === selectedNode.id
            ? { ...node, data: { ...node.data, meta: payload.meta } }
            : node,
        ),
      );
      setNotice(`${selectedNode.data.block.name} 已保存到左侧节点库`);
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    } finally {
      setSavingTemplate(false);
    }
  };
  const load = async () => {
    if (!savedId) return;
    try {
      const payload = await api<{ pipeline: Pipeline }>(
        `/pipelines/${savedId}`,
      );
      replacePipeline(payload.pipeline);
      await Promise.all([refreshVersions(savedId), refreshRuns(savedId)]);
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    }
  };
  const newWorkspace = () => {
    if (!blankTemplate) return;
    if (
      nodes.length > 0 &&
      !window.confirm("新建工作站会清空当前画布；未保存内容将丢失。继续吗？")
    )
      return;
    const next = {
      ...structuredClone(blankTemplate),
      id: `pipeline-${crypto.randomUUID().replaceAll("-", "").slice(0, 10)}`,
      name: "未命名工作流",
      revision: undefined,
      blocks: [],
      workflow: { nodes: [], edges: [] },
    };
    replacePipeline(next, "空白工作站已创建，可以从节点库添加第一个节点");
    setSavedId("");
    setVersions([]);
    setRuns([]);
    setInspectorTab("settings");
    setMobilePanel(null);
    setLibraryVisible(true);
    setInspectorVisible(true);
    setResultOpen(false);
    setError("");
  };
  const loadStandardMultifactorPreset = () => {
    const preset = presets.standard_multifactor;
    if (!preset) {
      setError("稳健多因子模板尚未从本地服务加载");
      return;
    }
    if (
      nodes.length > 0 &&
      !window.confirm("载入稳健多因子策略会替换当前画布；未保存内容将丢失。继续吗？")
    )
      return;
    const next = {
      ...structuredClone(preset),
      id: `standard-multifactor-${crypto.randomUUID().replaceAll("-", "").slice(0, 10)}`,
      revision: undefined,
    };
    replacePipeline(next, "已载入：标准稳健多因子");
    setSavedId("");
    setVersions([]);
    setRuns([]);
    setInspectorTab("settings");
    setResultOpen(false);
    setError("");
  };
  const duplicate = async () => {
    const current = serialize();
    if (!current) return;
    const next = {
      ...structuredClone(current),
      id: `pipeline-${crypto.randomUUID().replaceAll("-", "").slice(0, 10)}`,
      name: `${current.name} 副本`,
      revision: undefined,
    };
    replacePipeline(next, "正在复制并保存新工作流…");
    setSavedId("");
    setVersions([]);
    setRuns([]);
    setError("");
    try {
      const payload = await api<SavedPipeline>("/pipelines", {
        method: "POST",
        body: JSON.stringify({ pipeline: next, name: next.name }),
      });
      const savedCopy = { ...next, name: payload.name, revision: payload.revision };
      replacePipeline(savedCopy, `已复制并保存：${payload.name} · 版本 ${payload.revision}`);
      setSavedId(savedCopy.id);
      await Promise.all([refreshSaved(), refreshVersions(savedCopy.id)]);
    } catch (reason: unknown) {
      setError(`副本已保留在当前画布，但自动保存失败：${errorMessage(reason)}`);
      setNotice("副本尚未保存，请点击顶部“保存”后再离开页面");
    }
  };
  const loadVersion = async (revision: number) => {
    if (!pipeline) return;
    try {
      const payload = await api<{ pipeline: Pipeline }>(
        `/pipelines/${pipeline.id}/versions/${revision}`,
      );
      replacePipeline(
        { ...payload.pipeline, revision },
        `正在查看历史版本 ${revision}`,
      );
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    }
  };
  const restoreVersion = async (revision: number) => {
    if (!pipeline) return;
    try {
      await api(`/pipelines/${pipeline.id}/versions/${revision}/restore`, {
        method: "POST",
      });
      const payload = await api<{ pipeline: Pipeline }>(
        `/pipelines/${pipeline.id}`,
      );
      replacePipeline(
        payload.pipeline,
        `历史版本 ${revision} 已恢复为最新版本`,
      );
      await Promise.all([refreshSaved(), refreshVersions(pipeline.id)]);
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    }
  };
  const removePipeline = async () => {
    if (!pipeline || !saved.some((item) => item.id === pipeline.id)) {
      newWorkspace();
      return;
    }
    if (!window.confirm(`删除“${pipeline.name}”？历史文件会移入本地回收目录。`))
      return;
    try {
      await api(`/pipelines/${pipeline.id}`, { method: "DELETE" });
      await refreshSaved();
      newWorkspace();
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    }
  };
  const loadRun = async (runId: string) => {
    setRunning(true);
    setResultOpen(true);
    try {
      const payload = await api<BacktestResult>(`/runs/${runId}`);
      setResult(payload);
      setNotice(`已回看运行记录：${runId}`);
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    } finally {
      setRunning(false);
    }
  };
  const importJson = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    try {
      const raw = JSON.parse(await file.text());
      if (!raw || !Array.isArray(raw.blocks))
        throw new Error("JSON 中缺少 blocks 节点列表");
      const next: Pipeline = {
        ...raw,
        id: `pipeline-${crypto.randomUUID().replaceAll("-", "").slice(0, 10)}`,
        name: `${String(raw.name || "导入工作流")} 导入`,
        revision: undefined,
      };
      replacePipeline(next, `已导入：${file.name}`);
      setSavedId("");
      setVersions([]);
      setRuns([]);
    } catch (reason: unknown) {
      setError(`导入失败：${errorMessage(reason)}`);
    }
  };
  const download = () => {
    const current = serialize();
    if (!current) return;
    const anchor = document.createElement("a");
    anchor.href = URL.createObjectURL(
      new Blob([JSON.stringify(current, null, 2)], {
        type: "application/json",
      }),
    );
    anchor.download = `${current.id}.json`;
    anchor.click();
    URL.revokeObjectURL(anchor.href);
  };

  if (!pipeline)
    return (
      <div className="qflow-loading">
        <LoaderCircle className="spin" /> 正在连接量化工作流服务…
        {error && <span>{error}</span>}
      </div>
    );
  return (
    <section
      className={`qflow-studio ${libraryVisible ? "" : "library-closed"} ${inspectorVisible ? "" : "inspector-closed"}`}
      style={{ "--qflow-inspector-user-width": `${inspectorWidth}px` } as React.CSSProperties}
    >
      <header className="qflow-topbar">
        <div className="qflow-title">
          <Box />
          <div>
            <b>量构策略画布</b>
            <span>低代码节点编排 · 全市场回测 · 本地DuckDB</span>
          </div>
        </div>
        <label>
          当前工作流名称{" "}
          <input
            value={pipeline.name}
            onChange={(event) =>
              setPipeline({ ...pipeline, name: event.target.value })
            }
          />
        </label>
        <div className="qflow-actions">
          <button
            className={`qflow-panel-action ${libraryVisible || mobilePanel === "library" ? "active" : ""}`}
            title="打开或收起节点库"
            aria-pressed={libraryVisible || mobilePanel === "library"}
            onClick={() => togglePanel("library")}
          >
            <Box />
            节点库
          </button>
          <button
            className={`qflow-panel-action ${inspectorVisible || mobilePanel === "inspector" ? "active" : ""}`}
            title="打开或收起工作流配置"
            aria-pressed={inspectorVisible || mobilePanel === "inspector"}
            onClick={() => togglePanel("inspector")}
          >
            <Settings2 />
            配置
          </button>
          <button title="新建空白工作站" onClick={newWorkspace}>
            <FilePlus2 />
            新建
          </button>
          <button
            className="qflow-active-preset-button"
            title="载入不依赖外部门控的稳健多因子全市场模板"
            onClick={loadStandardMultifactorPreset}
            disabled={!presets.standard_multifactor}
          >
            <Activity />
            稳健多因子
          </button>
          <button title="校验工作流" onClick={validate}>
            <Check />
            校验
          </button>
          <button title="保存当前版本" onClick={save}>
            <Save />
            保存
          </button>
          <button className="qflow-primary" onClick={run} disabled={running}>
            {running ? <LoaderCircle className="spin" /> : <Play />}
            {running ? "运行中" : "运行"}
          </button>
        </div>
      </header>
      <div className="qflow-workspace">
        {mobilePanel && (
          <button
            className="qflow-mobile-backdrop"
            onClick={() => setMobilePanel(null)}
            aria-label="关闭侧栏"
          />
        )}
        <aside
          className={`qflow-library ${mobilePanel === "library" ? "mobile-open" : ""}`}
        >
          <section className="qflow-z-preset-card qflow-active-preset-card">
            <header>
              <span>M</span>
              <div>
                <b>标准稳健多因子</b>
                <small>硬筛 → 横截面排序 → 8只等权 → T+1</small>
              </div>
            </header>
            <ol>
              <li>剔除 ST、不可交易、涨停和流动性不足股票</li>
              <li>60日动量 + 10/60趋势 + 成交额排序</li>
              <li>Top20中取8只，10个交易日调仓</li>
              <li>单票≤10%，组合总仓≤80%</li>
            </ol>
            <button
              onClick={loadStandardMultifactorPreset}
              disabled={!presets.standard_multifactor}
            >
              <Activity /> 一键载入稳健模板
            </button>
            <small>所有因子都来自本地 OHLCV/成交额真实计算 · 仅回测/模拟 · 不接实盘</small>
          </section>
          <label className="qflow-search">
            <Search />
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="搜索节点"
            />
          </label>
          <div className="qflow-library-scroll">
            {STAGES.map((stage) => {
              const items = Object.entries(catalog).filter(
                ([type, meta]) =>
                  meta.category === stage &&
                  (!search ||
                    `${type}${meta.name}${meta.description}`
                      .toLowerCase()
                      .includes(search.toLowerCase())),
              );
              if (!items.length) return null;
              return (
                <section key={stage}>
                  <button
                    className="qflow-group"
                    onClick={() =>
                      setCollapsed((value) => ({
                        ...value,
                        [stage]: !value[stage],
                      }))
                    }
                  >
                    <span style={{ background: STAGE_META[stage].color }} />
                    <b>{STAGE_META[stage].label}</b>
                    <em>{items.length}</em>
                    <ChevronDown className={collapsed[stage] ? "closed" : ""} />
                  </button>
                  {!collapsed[stage] && (
                    <div>
                      {items.map(([type, meta]) => (
                        <button
                          className="qflow-palette-node"
                          draggable
                          onDragStart={(event) => {
                            event.dataTransfer.setData(
                              "application/quantweave-node",
                              type,
                            );
                            event.dataTransfer.effectAllowed = "move";
                          }}
                          onClick={() => {
                            addBlock(type);
                            showInspector();
                          }}
                          key={type}
                        >
                          <GripVertical />
                          <span>
                            <b>{meta.name}</b>
                            {meta.family && <em>{meta.family}</em>}
                            <small>{meta.description}</small>
                          </span>
                          {meta.available === false || meta.disabled_reason ? (
                            <i className="warn" />
                          ) : (
                            <i />
                          )}
                        </button>
                      ))}
                    </div>
                  )}
                </section>
              );
            })}
          </div>
        </aside>
        <main
          className="qflow-canvas"
          onDrop={onDrop}
          onDragOver={(event) => {
            event.preventDefault();
            event.dataTransfer.dropEffect = "move";
          }}
        >
          <ReactFlow<QuantNode, Edge>
            nodes={nodes}
            edges={edges}
            nodeTypes={NODE_TYPES}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            isValidConnection={isValidConnection}
            onInit={setFlow}
            onNodeClick={(_, node) => {
              setSelectedNodeId(node.id);
              showInspector();
            }}
            onPaneClick={() => setSelectedNodeId(null)}
            onNodesDelete={(deleted) => {
              const ids = new Set(deleted.map((node) => node.id));
              if (selectedNodeId && ids.has(selectedNodeId))
                setSelectedNodeId(null);
            }}
            fitView
            minZoom={0.2}
            maxZoom={1.8}
            deleteKeyCode={["Backspace", "Delete"]}
            defaultEdgeOptions={{ type: "smoothstep" }}
          >
            <Background
              variant={BackgroundVariant.Lines}
              gap={18}
              size={0.6}
              color="#233038"
            />
            <MiniMap
              pannable
              zoomable
              nodeColor={(node) =>
                STAGE_META[(node.data as QuantNodeData).block.category]
                  ?.color || "#50616b"
              }
            />
            <Controls showInteractive={false} />
          </ReactFlow>
          {!nodes.length && (
            <div className="qflow-empty-canvas">
              <Database />
              <b>空白工作站</b>
              <span>0 个节点 · 从节点库开始搭建</span>
              {notice && <small>{notice}</small>}
            </div>
          )}
          <div className="qflow-canvas-tools">
            <button onClick={autoLayout} title="自动排版">
              <RefreshCw />
            </button>
            <button onClick={standardWire} title="标准串联">
              <GitBranch />
            </button>
            <button
              onClick={() => flow?.fitView({ padding: 0.15, duration: 400 })}
              title="适应画布"
            >
              <ZoomIn />
            </button>
            <button onClick={() => setEdges([])} title="清空连线">
              <Trash2 />
            </button>
          </div>
          <div className="qflow-run-state">
            <Activity />
            <span>
              {running
                ? "正在执行全市场流水线"
                : `${nodes.length}个节点 · ${edges.length}条连接`}
            </span>
            {notice && <b>{notice}</b>}
            {error && <em>{error}</em>}
          </div>
        </main>
        <aside
          className={`qflow-inspector ${mobilePanel === "inspector" ? "mobile-open" : ""} ${resizingInspector ? "resizing" : ""}`}
        >
          <button
            className="qflow-inspector-resizer"
            aria-label="拖动调整右栏宽度"
            title="按住后向左拖动，放大因子讲解区；双击恢复默认宽度"
            onPointerDown={startInspectorResize}
            onDoubleClick={() => setInspectorWidth(520)}
          >
            <span />
          </button>
          {selectedNode ? (
            <>
              <div className="qflow-inspector-head">
                <span
                  style={{
                    background:
                      STAGE_META[selectedNode.data.block.category]?.color,
                  }}
                />
                <div>
                  <b>{selectedNode.data.block.name}</b>
                  <small>{selectedNode.data.block.type}</small>
                </div>
                <button onClick={() => setSelectedNodeId(null)}>
                  <X />
                </button>
              </div>
              <div className="qflow-inspector-scroll">
                <p>{selectedNode.data.meta.description}</p>
                {selectedNode.data.block.category === "factor" && (
                  <>
                    <dl className="qflow-factor-facts">
                      <div><dt>因子族</dt><dd>{selectedNode.data.meta.family || "自定义"}</dd></div>
                      <div><dt>计算公式</dt><dd><code>{String(selectedNode.data.meta.formula || selectedNode.data.block.params.formula || "由后端注册表计算")}</code></dd></div>
                      <div><dt>所需数据</dt><dd>{selectedNode.data.meta.required_fields?.join("、") || "本地日线行情"}</dd></div>
                      <div><dt>时点安全</dt><dd>{selectedNode.data.meta.point_in_time === false ? "否" : "是 · 只用当日及此前数据"}</dd></div>
                    </dl>
                    <FactorTeachingPanel block={selectedNode.data.block} meta={selectedNode.data.meta}/>
                  </>
                )}
                {selectedNode.data.meta.disabled_reason && (
                  <div className="qflow-warning">
                    {selectedNode.data.meta.disabled_reason}
                  </div>
                )}
                <label>
                  <span>节点名称</span>
                  <input
                    value={selectedNode.data.block.name}
                    onChange={(event) =>
                      updateSelectedBlock((block) => ({
                        ...block,
                        name: event.target.value,
                        params: isCustomFormulaBlock(block.type)
                          ? { ...block.params, name: event.target.value }
                          : block.params,
                      }))
                    }
                  />
                </label>
                <label className="qflow-toggle-row">
                  <span>启用节点</span>
                  <button
                    className={`qflow-switch ${selectedNode.data.block.enabled ? "on" : ""}`}
                    disabled={
                      selectedNode.data.meta.available === false ||
                      Boolean(selectedNode.data.meta.disabled_reason)
                    }
                    onClick={() =>
                      updateSelectedBlock((block) => ({
                        ...block,
                        enabled: !block.enabled,
                      }))
                    }
                  >
                    <i />
                  </button>
                </label>
                {["filter", "strategy", "factor", "penalty"].includes(
                  selectedNode.data.block.category,
                ) && (
                  <label>
                    <span>多节点逻辑</span>
                    <ParamInput
                      name="combine"
                      value={selectedNode.data.block.combine}
                      onChange={(value) =>
                        updateSelectedBlock((block) => ({
                          ...block,
                          combine: value === "OR" ? "OR" : "AND",
                        }))
                      }
                    />
                  </label>
                )}
                <h3>节点参数</h3>
                {Object.entries(selectedNode.data.block.params)
                  .filter(([name]) => !["name", "group"].includes(name))
                  .map(([name, value]) => (
                    <label key={name}>
                      <span>{paramLabel(name)}</span>
                      <ParamInput
                        name={name}
                        value={value}
                        blockType={selectedNode.data.block.type}
                        onChange={(next) =>
                          updateSelectedBlock((block) => ({
                            ...block,
                            params: { ...block.params, [name]: next },
                          }))
                        }
                      />
                    </label>
                  ))}
                {isCustomFormulaBlock(selectedNode.data.block.type) && (
                  <button
                    className="qflow-wide-button qflow-template-save"
                    onClick={saveSelectedAsTemplate}
                    disabled={savingTemplate}
                  >
                    <Save />
                    {savingTemplate
                      ? "保存中"
                      : selectedNode.data.meta.template_id
                        ? "更新左侧模块"
                        : "保存到左侧节点库"}
                  </button>
                )}
              </div>
              <button
                className="qflow-delete"
                onClick={() => {
                  setNodes((current) =>
                    current.filter((node) => node.id !== selectedNode.id),
                  );
                  setEdges((current) =>
                    current.filter(
                      (edge) =>
                        edge.source !== selectedNode.id &&
                        edge.target !== selectedNode.id,
                    ),
                  );
                  setSelectedNodeId(null);
                }}
              >
                <Trash2 />
                删除节点
              </button>
            </>
          ) : (
            <>
              <div className="qflow-inspector-head">
                <Settings2 />
                <div>
                  <b>{pipeline.name}</b>
                  <small>
                    {pipeline.revision
                      ? `已保存版本 ${pipeline.revision}`
                      : "未保存草稿"}
                  </small>
                </div>
              </div>
              <div className="qflow-inspector-tabs">
                <button
                  className={inspectorTab === "settings" ? "active" : ""}
                  onClick={() => setInspectorTab("settings")}
                >
                  <Settings2 />
                  设置
                </button>
                <button
                  className={inspectorTab === "versions" ? "active" : ""}
                  onClick={() => {
                    setInspectorTab("versions");
                    refreshVersions(pipeline.id).catch((reason) =>
                      setError(errorMessage(reason)),
                    );
                  }}
                >
                  <History />
                  版本
                </button>
                <button
                  className={inspectorTab === "runs" ? "active" : ""}
                  onClick={() => {
                    setInspectorTab("runs");
                    refreshRuns(pipeline.id).catch((reason) =>
                      setError(errorMessage(reason)),
                    );
                  }}
                >
                  <CirclePlay />
                  运行
                </button>
              </div>
              {inspectorTab === "settings" && (
                <div className="qflow-inspector-scroll">
                  <label>
                    <span>说明</span>
                    <textarea
                      value={pipeline.description}
                      onChange={(event) =>
                        setPipeline({
                          ...pipeline,
                          description: event.target.value,
                        })
                      }
                    />
                  </label>
                  <label>
                    <span>开始日期</span>
                    <input
                      type="date"
                      value={pipeline.start}
                      onChange={(event) =>
                        setPipeline({ ...pipeline, start: event.target.value })
                      }
                    />
                  </label>
                  <label>
                    <span>结束日期</span>
                    <input
                      type="date"
                      value={pipeline.end}
                      onChange={(event) =>
                        setPipeline({ ...pipeline, end: event.target.value })
                      }
                    />
                  </label>
                  <label>
                    <span>初始资金</span>
                    <input
                      type="number"
                      value={pipeline.initial_cash}
                      onChange={(event) =>
                        setPipeline({
                          ...pipeline,
                          initial_cash: Number(event.target.value),
                        })
                      }
                    />
                  </label>
                  <h3>策略组合</h3>
                  <label>
                    <span>已保存工作流</span>
                    <select
                      value={savedId}
                      onChange={(event) => setSavedId(event.target.value)}
                    >
                      <option value="">请选择</option>
                      {saved.map((item) => (
                        <option value={item.id} key={item.id}>
                          {item.name}
                          {` · v${item.revision || 1}`}
                          {item.updated_at ? ` · ${item.updated_at.slice(5, 16).replace("T", " ")}` : ""}
                          {item.runnable === false ? " · 草稿" : ""}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    className="qflow-wide-button"
                    onClick={load}
                    disabled={!savedId}
                  >
                    <Upload />
                    载入工作流
                  </button>
                  <button className="qflow-wide-button" onClick={duplicate}>
                    <Copy />
                    复制为新工作流
                  </button>
                  <button
                    className="qflow-wide-button"
                    onClick={() => importRef.current?.click()}
                  >
                    <FileUp />
                    导入 JSON
                  </button>
                  <input
                    ref={importRef}
                    className="qflow-file-input"
                    type="file"
                    accept="application/json,.json"
                    onChange={importJson}
                  />
                  <button className="qflow-wide-button" onClick={download}>
                    <Download />
                    导出 JSON
                  </button>
                  <button
                    className="qflow-wide-button qflow-danger-button"
                    onClick={removePipeline}
                  >
                    <Trash2 />
                    删除工作流
                  </button>
                </div>
              )}
              {inspectorTab === "versions" && (
                <div className="qflow-inspector-scroll qflow-history-list">
                  {versions.length ? (
                    versions.map((item) => (
                      <div className="qflow-history-entry" key={item.revision}>
                        <button
                          onClick={() => loadVersion(Number(item.revision))}
                        >
                          <span>
                            <b>版本 {item.revision}</b>
                            <small>
                              {item.updated_at?.replace("T", " ") || "--"}
                            </small>
                          </span>
                          <em>{item.blocks || 0} 节点</em>
                        </button>
                        <button
                          title="恢复为最新版本"
                          onClick={() => restoreVersion(Number(item.revision))}
                        >
                          <RotateCcw />
                        </button>
                      </div>
                    ))
                  ) : (
                    <div className="qflow-side-empty">暂无保存版本</div>
                  )}
                </div>
              )}
              {inspectorTab === "runs" && (
                <div className="qflow-inspector-scroll qflow-history-list">
                  {runs.length ? (
                    runs.map((item) => (
                      <button
                        className="qflow-run-history"
                        onClick={() => loadRun(item.run_id)}
                        key={item.run_id}
                      >
                        <span>
                          <b>{item.run_id}</b>
                          <small>{item.created_at?.replace("T", " ")}</small>
                        </span>
                        <em>
                          {formatMetric(
                            "total_return",
                            item.metrics?.total_return,
                          )}
                        </em>
                      </button>
                    ))
                  ) : (
                    <div className="qflow-side-empty">暂无运行记录</div>
                  )}
                </div>
              )}
            </>
          )}
        </aside>
      </div>
      <ResultDrawer
        key={`${result?.run_id || "empty"}-${running}`}
        open={resultOpen}
        running={running}
        result={result}
        error={error}
        onClose={() => setResultOpen(false)}
      />
    </section>
  );
}
