"use client";

import type { Chart as KLineChartInstance, Crosshair, KLineData } from "klinecharts";
import { useEffect, useMemo, useRef, useState } from "react";

type Candle = { date: string; open: number; close: number; high: number; low: number; volume: number; amount: number };
type ChartViewport = {
  start: number;
  end: number;
  from?: number;
  to?: number;
  x0?: number;
  barWidth?: number;
};
type MainIndicator = "MA" | "BBI" | "CYC";
type ChartSettings = {
  maShort: number;
  maMid: number;
  maLong: number;
};
type KLineApi = typeof import("klinecharts");

declare global {
  interface Window {
    klinecharts?: KLineApi;
  }
}

const CANDLE_PANE = "candle_pane";
const RANGE_GROUP = "quantweave-range-selection";
const timestampFor = (date: string) => new Date(`${date}T00:00:00+08:00`).getTime();
const formatAmount = (value: number) => value >= 1e8 ? `${(value / 1e8).toFixed(2)}亿` : value >= 1e4 ? `${(value / 1e4).toFixed(2)}万` : value.toFixed(2);
const formatSignedPct = (value: number) => `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
const candleDataSignature = (data: Candle[]) => {
  let hash = 2166136261;
  for (const item of data) {
    const token = `${item.date}|${item.open}|${item.high}|${item.low}|${item.close}|${item.volume}|${item.amount};`;
    for (let index = 0; index < token.length; index += 1) {
      hash ^= token.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
  }
  return `${data.length}:${hash >>> 0}`;
};

const candleTooltipLegends = ({ prev, current }: { prev: KLineData | null; current: KLineData | null }) => {
  if (!current) return [];
  const close = Number(current.close);
  const previousClose = Number(prev?.close);
  const comparison = Number.isFinite(previousClose) && previousClose !== 0
    ? previousClose
    : Number(current.open);
  const change = close - comparison;
  const changePct = comparison ? change / comparison * 100 : Number.NaN;
  const changeColor = change >= 0 ? "#f05a63" : "#23b985";
  const precision = close < 10 ? 3 : 2;
  const value = (number: number) => Number.isFinite(number) ? number.toFixed(precision) : "--";
  return [
    { title: "日期: ", value: String(current.date || "--") },
    { title: "开: ", value: value(Number(current.open)) },
    { title: "高: ", value: { text: value(Number(current.high)), color: "#f05a63" } },
    { title: "低: ", value: { text: value(Number(current.low)), color: "#23b985" } },
    { title: "收: ", value: value(close) },
    { title: "涨跌: ", value: { text: `${change >= 0 ? "+" : ""}${value(change)}`, color: changeColor } },
    { title: "涨跌幅: ", value: { text: Number.isFinite(changePct) ? formatSignedPct(changePct) : "--", color: changeColor } },
    { title: "量: ", value: formatAmount(Number(current.volume) || 0) },
    { title: "额: ", value: formatAmount(Number(current.turnover) || 0) },
  ];
};

let customIndicatorsReady = false;
function registerQuantWeaveIndicators(api: KLineApi) {
  if (customIndicatorsReady) return;
  customIndicatorsReady = true;
  api.registerIndicator({
    name: "QUANTWEAVE_CYC",
    shortName: "CYC",
    series: "price",
    precision: 2,
    shouldOhlc: true,
    figures: [
      { key: "cyc5", title: "CYC5: ", type: "line", styles: () => ({ color: "#e5d15b" }) },
      { key: "cyc13", title: "CYC13: ", type: "line", styles: () => ({ color: "#67a8f5" }) },
      { key: "cyc34", title: "CYC34: ", type: "line", styles: () => ({ color: "#b76ae8" }) },
      { key: "cycInf", title: "CYC∞: ", type: "line", styles: () => ({ color: "#f0a443" }) },
    ],
    calc: dataList => {
      const periods = [5, 13, 34];
      const rollingAmount = periods.map(() => 0);
      const rollingVolume = periods.map(() => 0);
      let totalAmount = 0;
      let totalVolume = 0;
      return dataList.map((bar, index) => {
        const amount = Number(bar.turnover) || 0;
        const volume = Number(bar.volume) || 0;
        totalAmount += amount;
        totalVolume += volume;
        const values: Record<string, number> = {};
        periods.forEach((period, periodIndex) => {
          rollingAmount[periodIndex] += amount;
          rollingVolume[periodIndex] += volume;
          if (index >= period) {
            rollingAmount[periodIndex] -= Number(dataList[index - period].turnover) || 0;
            rollingVolume[periodIndex] -= Number(dataList[index - period].volume) || 0;
          }
          if (index >= period - 1 && rollingVolume[periodIndex] > 0) {
            values[`cyc${period}`] = rollingAmount[periodIndex] / (rollingVolume[periodIndex] * 100);
          }
        });
        if (totalVolume > 0) values.cycInf = totalAmount / (totalVolume * 100);
        return values;
      });
    },
  });
}

const chartStyles = {
  grid: {
    horizontal: { show: true, color: "#26323a", size: 1, style: "dashed", dashedValue: [3, 3] },
    vertical: { show: true, color: "#202b32", size: 1, style: "dashed", dashedValue: [3, 3] },
  },
  candle: {
    bar: {
      upColor: "#f05a63",
      downColor: "#23b985",
      noChangeColor: "#98a6ae",
      upBorderColor: "#f05a63",
      downBorderColor: "#23b985",
      noChangeBorderColor: "#98a6ae",
      upWickColor: "#f05a63",
      downWickColor: "#23b985",
      noChangeWickColor: "#98a6ae",
    },
    priceMark: {
      high: { color: "#b9c6cd" },
      low: { color: "#b9c6cd" },
      last: {
        upColor: "#f05a63",
        downColor: "#23b985",
        noChangeColor: "#98a6ae",
      },
    },
    tooltip: {
      showRule: "follow_cross",
      showType: "standard",
      legend: { template: candleTooltipLegends },
      text: { color: "#c8d4da", size: 11, family: "ui-monospace, SFMono-Regular, Menlo, monospace" },
    },
  },
  indicator: {
    tooltip: {
      showRule: "follow_cross",
      showType: "standard",
      text: { color: "#9fb0ba", size: 10, family: "ui-monospace, SFMono-Regular, Menlo, monospace" },
    },
    lines: [
      { color: "#e5d15b" },
      { color: "#67a8f5" },
      { color: "#b76ae8" },
      { color: "#f0a443" },
    ],
  },
  xAxis: {
    axisLine: { color: "#34414a" },
    tickLine: { color: "#34414a" },
    tickText: { color: "#71838e", size: 10, family: "ui-monospace, SFMono-Regular, Menlo, monospace" },
  },
  yAxis: {
    axisLine: { color: "#34414a" },
    tickLine: { color: "#34414a" },
    tickText: { color: "#8fa0a9", size: 10, family: "ui-monospace, SFMono-Regular, Menlo, monospace" },
  },
  crosshair: {
    horizontal: {
      line: { color: "#c8d4da", size: 1, style: "dashed", dashedValue: [4, 3] },
      text: { color: "#e8eef1", backgroundColor: "#45545d" },
    },
    vertical: {
      line: { color: "#c8d4da", size: 1, style: "dashed", dashedValue: [4, 3] },
      text: { color: "#e8eef1", backgroundColor: "#45545d" },
    },
  },
} as const;

function applyMainIndicator(chart: KLineChartInstance, mainIndicator: MainIndicator, settings: ChartSettings) {
  chart.removeIndicator({ paneId: CANDLE_PANE });
  if (mainIndicator === "MA") {
    chart.createIndicator({ name: "MA", paneId: CANDLE_PANE, calcParams: [settings.maShort, settings.maMid, settings.maLong] }, true);
  } else if (mainIndicator === "BBI") {
    chart.createIndicator({ name: "BBI", paneId: CANDLE_PANE, calcParams: [3, 6, 12, 24] }, true);
  } else if (mainIndicator === "CYC") {
    chart.createIndicator({ name: "QUANTWEAVE_CYC", paneId: CANDLE_PANE }, true);
  }
}

function loadKLineApi(): Promise<KLineApi> {
  if (window.klinecharts) return Promise.resolve(window.klinecharts);
  return new Promise((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>('script[data-quantweave-klinecharts="true"]');
    const finish = () => window.klinecharts ? resolve(window.klinecharts) : reject(new Error("静态资源已载入但未找到 KLineChart"));
    if (existing) {
      if (existing.dataset.loaded === "true") finish();
      else {
        existing.addEventListener("load", finish, { once: true });
        existing.addEventListener("error", () => reject(new Error("KLineChart 静态资源加载失败")), { once: true });
      }
      return;
    }
    const script = document.createElement("script");
    script.src = "/vendor/klinecharts.min.js";
    script.async = true;
    script.dataset.quantweaveKlinecharts = "true";
    script.addEventListener("load", () => { script.dataset.loaded = "true"; finish(); }, { once: true });
    script.addEventListener("error", () => reject(new Error("KLineChart 静态资源加载失败")), { once: true });
    document.head.appendChild(script);
  });
}

export default function MatureKlineChart({
  data,
  settings,
  mainIndicator,
  viewportChange,
  linkedHover,
  hoverChange,
  focusDate,
  height = 250,
}: {
  data: Candle[];
  settings: ChartSettings;
  mainIndicator: MainIndicator;
  viewportChange?: (value: ChartViewport) => void;
  linkedHover?: number | null;
  hoverChange?: (index: number | null) => void;
  focusDate?: string;
  height?: number;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<KLineChartInstance | null>(null);
  const apiRef = useRef<KLineApi | null>(null);
  const dataRef = useRef<KLineData[]>([]);
  const visibleRangeRef = useRef<ChartViewport>({ start: 0, end: 0 });
  const followLatestRef = useRef(true);
  const hoverChangeRef = useRef(hoverChange);
  const viewportChangeRef = useRef(viewportChange);
  const rangeModeRef = useRef(false);
  const rangeStartRef = useRef<number | null>(null);
  const crosshairIndexRef = useRef<number | null>(null);
  const [rangeMode, setRangeMode] = useState(false);
  const [rangeStart, setRangeStart] = useState<number | null>(null);
  const [rangeEnd, setRangeEnd] = useState<number | null>(null);
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  const [runtimeError, setRuntimeError] = useState("");
  const [rangeBenchmarks, setRangeBenchmarks] = useState<{ sh?: number; sz?: number; loading: boolean }>({ loading: false });
  const dataKey = useMemo(() => candleDataSignature(data), [data]);
  const mapped = useMemo<KLineData[]>(() => data.map(item => ({
    timestamp: timestampFor(item.date),
    open: Number(item.open),
    high: Number(item.high),
    low: Number(item.low),
    close: Number(item.close),
    volume: Number(item.volume) || 0,
    turnover: Number(item.amount) || 0,
    date: item.date,
  })), [data]);
  const hasData = mapped.length > 0;

  useEffect(() => { hoverChangeRef.current = hoverChange; }, [hoverChange]);
  useEffect(() => { viewportChangeRef.current = viewportChange; }, [viewportChange]);
  useEffect(() => { rangeModeRef.current = rangeMode; }, [rangeMode]);
  useEffect(() => { rangeStartRef.current = rangeStart; }, [rangeStart]);

  const readViewport = (chart: KLineChartInstance, length: number): ChartViewport => {
    const range = chart.getVisibleRange();
    const hostWidth = hostRef.current?.clientWidth || 0;
    const pixels = chart.convertToPixel(
      [{ dataIndex: 0 }, { dataIndex: 1 }],
      { paneId: CANDLE_PANE },
    ) as Array<{ x?: number }>;
    const firstX = Number(pixels[0]?.x);
    const secondX = Number(pixels[1]?.x);
    const hasPixelScale = hostWidth > 0 && Number.isFinite(firstX) && Number.isFinite(secondX);
    return {
      start: Math.max(0, Math.floor(range.from)),
      end: Math.min(length, Math.ceil(range.to) + 1),
      from: range.realFrom,
      to: range.realTo,
      x0: hasPixelScale ? firstX / hostWidth : undefined,
      barWidth: hasPixelScale ? (secondX - firstX) / hostWidth : undefined,
    };
  };

  useEffect(() => {
    const host = hostRef.current;
    if (!hasData || !host) return;
    let cancelled = false;
    let chart: KLineChartInstance | null = null;
    let observer: ResizeObserver | null = null;
    let resizeFrame: number | null = null;
    let lastHostWidth = 0;
    let lastHostHeight = 0;
    let syncViewport: (() => void) | null = null;
    let crosshairHandler: ((payload?: unknown) => void) | null = null;
    let rangeClickHandler: ((event: MouseEvent) => void) | null = null;
    void loadKLineApi().then(api => {
      if (cancelled || !hostRef.current) return;
      apiRef.current = api;
      registerQuantWeaveIndicators(api);
      chart = api.init(hostRef.current, {
        locale: "zh-CN",
        timezone: "Asia/Shanghai",
        styles: chartStyles as never,
        zoomAnchor: { main: "cursor", xAxis: "cursor" },
      });
      if (!chart) return;
      chartRef.current = chart;
      chart.setOffsetRightDistance(8);
      chart.setMaxOffsetLeftDistance(16);
      chart.setMaxOffsetRightDistance(16);
      chart.setZoomEnabled(true);
      chart.setScrollEnabled(true);
      syncViewport = () => {
        if (!chart) return;
        const viewport = readViewport(chart, dataRef.current.length);
        visibleRangeRef.current = viewport;
        followLatestRef.current = viewport.end >= dataRef.current.length - 2;
        viewportChangeRef.current?.(viewport);
      };
      crosshairHandler = (payload?: unknown) => {
        const crosshair = payload as Crosshair | undefined;
        const index = crosshair?.realDataIndex ?? crosshair?.dataIndex;
        crosshairIndexRef.current = typeof index === "number" && index >= 0 ? index : null;
        const nextIndex = crosshairIndexRef.current;
        requestAnimationFrame(() => { if (!cancelled) setHoverIndex(nextIndex); });
        hoverChangeRef.current?.(crosshairIndexRef.current);
      };
      rangeClickHandler = event => {
        if (!rangeModeRef.current) return;
        if (!chart || !hostRef.current) return;
        const bounds = hostRef.current.getBoundingClientRect();
        const x = Number.isFinite(event.offsetX) ? event.offsetX : event.clientX - bounds.left;
        const y = Number.isFinite(event.offsetY) ? event.offsetY : event.clientY - bounds.top;
        const points = chart.convertFromPixel([{ x, y }], { paneId: CANDLE_PANE }) as Array<{ dataIndex?: number }>;
        const rawIndex = points[0]?.dataIndex ?? crosshairIndexRef.current;
        if (typeof rawIndex !== "number") return;
        const index = Math.max(0, Math.min(dataRef.current.length - 1, Math.round(rawIndex)));
        if (rangeStartRef.current === null) {
          setRangeStart(index);
          setRangeEnd(null);
        } else {
          setRangeEnd(index);
          setRangeMode(false);
        }
      };
      chart.subscribeAction("onVisibleRangeChange", syncViewport);
      chart.subscribeAction("onCrosshairChange", crosshairHandler);
      hostRef.current.addEventListener("click", rangeClickHandler);
      observer = new ResizeObserver(() => {
        // Coalesce chart layout changes. Calling resize synchronously from a
        // ResizeObserver callback can trigger the browser's loop warning.
        if (resizeFrame !== null) return;
        resizeFrame = requestAnimationFrame(() => {
          resizeFrame = null;
          const host = hostRef.current;
          if (!chart || !host) return;
          const rect = host.getBoundingClientRect();
          if (rect.width === lastHostWidth && rect.height === lastHostHeight) return;
          lastHostWidth = rect.width;
          lastHostHeight = rect.height;
          chart.resize();
          requestAnimationFrame(() => syncViewport?.());
        });
      });
      observer.observe(hostRef.current);
      if (dataRef.current.length) {
        chart.setDataLoader({ getBars: ({ callback }) => callback(dataRef.current, { backward: false, forward: false }) });
        const precision = dataRef.current.slice(-30).some(item => item.close < 10) ? 3 : 2;
        chart.setSymbol({
          ticker: `quantweave-${dataRef.current[0].timestamp}-${dataRef.current.at(-1)?.timestamp}-${dataRef.current.length}`,
          pricePrecision: precision,
          volumePrecision: 0,
        });
        chart.setPeriod({ type: "day", span: 1 });
        chart.resetData();
        const width = hostRef.current.clientWidth || 900;
        chart.setBarSpace(Math.max(2.5, Math.min(22, (width - 68) / Math.min(90, Math.max(dataRef.current.length, 20)))));
        applyMainIndicator(chart, mainIndicator, settings);
        requestAnimationFrame(() => {
          chart?.scrollToRealTime();
          followLatestRef.current = true;
          requestAnimationFrame(() => syncViewport?.());
        });
      }
    }).catch(error => {
      if (!cancelled) setRuntimeError(error instanceof Error ? error.message : String(error));
    });
    return () => {
      cancelled = true;
      observer?.disconnect();
      if (resizeFrame !== null) cancelAnimationFrame(resizeFrame);
      if (chart && syncViewport) chart.unsubscribeAction("onVisibleRangeChange", syncViewport);
      if (chart && crosshairHandler) chart.unsubscribeAction("onCrosshairChange", crosshairHandler);
      if (rangeClickHandler) host.removeEventListener("click", rangeClickHandler);
      if (chart) apiRef.current?.dispose(chart);
      chartRef.current = null;
    };
  }, [hasData, mainIndicator, settings]);

  useEffect(() => {
    const previousData = dataRef.current;
    const previousViewport = visibleRangeRef.current;
    const preserveTimestamp = !followLatestRef.current && previousData.length
      ? previousData[Math.max(0, Math.min(previousData.length - 1, previousViewport.start))]?.timestamp
      : undefined;
    dataRef.current = mapped;
    const chart = chartRef.current;
    if (!chart || !mapped.length) return;
    chart.setDataLoader({
      getBars: ({ callback }) => callback(dataRef.current, { backward: false, forward: false }),
    });
    const decimals = Math.max(...data.slice(-30).map(item => item.close < 10 ? 3 : 2), 2);
    chart.setSymbol({
      ticker: `quantweave-${data[0]?.date}-${data.at(-1)?.date}-${data.length}`,
      pricePrecision: decimals,
      volumePrecision: 0,
    });
    chart.setPeriod({ type: "day", span: 1 });
    chart.resetData();
    const width = hostRef.current?.clientWidth ?? 900;
    chart.setMaxOffsetLeftDistance(16);
    chart.setMaxOffsetRightDistance(16);
    chart.setBarSpace(Math.max(2.5, Math.min(22, (width - 68) / Math.min(90, Math.max(mapped.length, 20)))));
    requestAnimationFrame(() => {
      if (focusDate) {
        chart.scrollToTimestamp(timestampFor(focusDate));
        followLatestRef.current = false;
      } else if (preserveTimestamp) {
        chart.scrollToTimestamp(preserveTimestamp);
      } else {
        chart.scrollToRealTime();
        followLatestRef.current = true;
      }
      const viewport = readViewport(chart, mapped.length);
      visibleRangeRef.current = viewport;
      viewportChangeRef.current?.(viewport);
    });
    setRangeMode(false);
    setRangeStart(null);
    setRangeEnd(null);
    setHoverIndex(null);
  }, [dataKey, focusDate, data, mapped]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !mapped.length) return;
    applyMainIndicator(chart, mainIndicator, settings);
  }, [mapped, mainIndicator, settings]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || linkedHover === undefined) return;
    if (linkedHover === null) {
      requestAnimationFrame(() => setHoverIndex(null));
      chart.executeAction("onCrosshairChange", null as never);
      return;
    }
    if (!mapped[linkedHover]) return;
    requestAnimationFrame(() => setHoverIndex(linkedHover));
    chart.executeAction("onCrosshairChange", {
      paneId: CANDLE_PANE,
      dataIndex: linkedHover,
      timestamp: mapped[linkedHover].timestamp,
      kLineData: mapped[linkedHover],
    });
  }, [linkedHover, mapped]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    chart.removeOverlay({ groupId: RANGE_GROUP });
    [rangeStart, rangeEnd].forEach((index, lineIndex) => {
      if (index === null || !mapped[index]) return;
      chart.createOverlay({
        id: `${RANGE_GROUP}-${lineIndex}`,
        groupId: RANGE_GROUP,
        name: "verticalStraightLine",
        paneId: CANDLE_PANE,
        lock: true,
        points: [{ timestamp: mapped[index].timestamp }],
        styles: {
          line: { color: lineIndex === 0 ? "#f0a443" : "#67a8f5", size: 1, style: "dashed", dashedValue: [5, 3] },
        },
      });
    });
  }, [rangeStart, rangeEnd, mapped]);

  const from = rangeStart === null ? null : Math.min(rangeStart, rangeEnd ?? rangeStart);
  const to = rangeStart === null ? null : Math.max(rangeStart, rangeEnd ?? rangeStart);
  const rangeRows = rangeEnd === null || from === null || to === null ? [] : data.slice(from, to + 1);
  const rangeFirst = rangeRows[0];
  const rangeLast = rangeRows.at(-1);
  const rangeVolume = rangeRows.reduce((sum, row) => sum + (Number(row.volume) || 0), 0);
  const rangeAmount = rangeRows.reduce((sum, row) => sum + (Number(row.amount) || 0), 0);
  const rangeHigh = rangeRows.length ? Math.max(...rangeRows.map(row => row.high)) : 0;
  const rangeLow = rangeRows.length ? Math.min(...rangeRows.map(row => row.low)) : 0;
  const rangeRise = rangeFirst && rangeLast && rangeFirst.open ? (rangeLast.close / rangeFirst.open - 1) * 100 : 0;
  const rangeAmplitude = rangeLow ? (rangeHigh / rangeLow - 1) * 100 : 0;
  const rangeMaxRise = rangeFirst?.open ? (rangeHigh / rangeFirst.open - 1) * 100 : 0;
  const rangeAverage = rangeVolume ? rangeAmount / (rangeVolume * 100) : 0;
  const rangeBalance = rangeRows.length ? rangeRows.reduce((sum, row) => sum + (row.high + row.low + row.close) / 3, 0) / rangeRows.length : 0;
  const dailyInfoIndex = hoverIndex !== null && data[hoverIndex] ? hoverIndex : data.length - 1;
  const dailyInfo = data[dailyInfoIndex];
  const dailyPreviousClose = dailyInfoIndex > 0 ? data[dailyInfoIndex - 1]?.close : undefined;
  const dailyChange = dailyInfo && dailyPreviousClose !== undefined ? dailyInfo.close - dailyPreviousClose : undefined;
  const dailyChangePct = dailyChange !== undefined && dailyPreviousClose ? dailyChange / dailyPreviousClose * 100 : undefined;
  const dailyPrecision = dailyInfo?.close < 10 ? 3 : 2;
  const adjustBarSpace = (scale: number) => {
    const chart = chartRef.current;
    if (!chart) return;
    const current = chart.getBarSpace().bar;
    chart.setBarSpace(Math.max(2, Math.min(40, current * scale)));
  };
  const fitBars = () => {
    const chart = chartRef.current;
    const width = hostRef.current?.clientWidth || 900;
    if (!chart) return;
    chart.setBarSpace(Math.max(2.5, Math.min(22, (width - 68) / Math.min(100, Math.max(mapped.length, 20)))));
  };
  let rangePeak = 0;
  let rangeMaxDrawdown = 0;
  rangeRows.forEach(row => {
    rangePeak = Math.max(rangePeak, row.high);
    if (rangePeak) rangeMaxDrawdown = Math.min(rangeMaxDrawdown, (row.low / rangePeak - 1) * 100);
  });

  useEffect(() => {
    if (!rangeFirst || !rangeLast) {
      window.setTimeout(() => setRangeBenchmarks({ loading: false }), 0);
      return;
    }
    const controller = new AbortController();
    window.setTimeout(() => setRangeBenchmarks({ loading: true }), 0);
    const load = async (code: string, market: number) => {
      const response = await fetch(`/api/kline?code=${code}&market=${market}&period=日线&adjustment=前复权`, { signal: controller.signal });
      if (!response.ok) throw new Error("benchmark");
      const rows: Candle[] = (await response.json()).candles || [];
      const selected = rows.filter(row => row.date >= rangeFirst.date && row.date <= rangeLast.date);
      return selected.length && selected[0].open ? (selected.at(-1)!.close / selected[0].open - 1) * 100 : undefined;
    };
    Promise.all([load("000001", 1), load("399001", 0)])
      .then(([sh, sz]) => setRangeBenchmarks({ sh, sz, loading: false }))
      .catch(() => { if (!controller.signal.aborted) setRangeBenchmarks({ loading: false }); });
    return () => controller.abort();
  }, [rangeFirst, rangeLast]);

  if (!data.length) return <div className="chart-loading">正在载入 K 线…</div>;

  return (
    <div className={`chart-shell mature-kline-shell ${rangeMode ? "range-selecting" : ""}`} style={{ height }}>
      <div className="mature-kline-brand">KLineChart <span>{mainIndicator}</span></div>
      <div className="zoom-tools">
        <button onClick={() => adjustBarSpace(0.72)} aria-label="缩小">－</button>
        <button onClick={fitBars}>适配</button>
        <button onClick={() => { followLatestRef.current = true; chartRef.current?.scrollToRealTime(120); }}>最新</button>
        <button onClick={() => adjustBarSpace(1.38)} aria-label="放大">＋</button>
      </div>
      <div className="range-tools">
        <button
          className={rangeMode ? "active" : ""}
          onClick={() => {
            if (rangeMode) {
              setRangeMode(false);
              return;
            }
            setRangeStart(null);
            setRangeEnd(null);
            setRangeMode(true);
          }}
        >
          {rangeMode ? (rangeStart === null ? "点击区间起点" : "点击区间终点") : "区间统计"}
        </button>
        {rangeStart !== null && <button onClick={() => { setRangeMode(false); setRangeStart(null); setRangeEnd(null); }}>清除</button>}
      </div>
      <div className="pan-hint">滚轮缩放 · 左右拖动查看历史 · 十字线同步主副图</div>
      {runtimeError && <div className="mature-kline-error">KLineChart 加载失败：{runtimeError}</div>}
      {dailyInfo && <div className="mature-kline-daily-info"><b>{dailyInfo.date}</b><span>开 <strong>{dailyInfo.open.toFixed(dailyPrecision)}</strong></span><span>高 <strong className="up">{dailyInfo.high.toFixed(dailyPrecision)}</strong></span><span>低 <strong className="down">{dailyInfo.low.toFixed(dailyPrecision)}</strong></span><span>收 <strong>{dailyInfo.close.toFixed(dailyPrecision)}</strong></span><span>涨跌 <strong className={(dailyChange ?? 0)>=0?'up':'down'}>{dailyChange===undefined?'--':`${dailyChange>=0?'+':''}${dailyChange.toFixed(dailyPrecision)}`}</strong></span><span>涨跌幅 <strong className={(dailyChangePct ?? 0)>=0?'up':'down'}>{dailyChangePct===undefined?'--':formatSignedPct(dailyChangePct)}</strong></span><span>量 <strong>{formatAmount(Number(dailyInfo.volume)||0)}</strong></span><span>额 <strong>{formatAmount(Number(dailyInfo.amount)||0)}</strong></span></div>}
      <div className="mature-kline-canvas" ref={hostRef} />
      {rangeRows.length > 0 && rangeFirst && rangeLast && (
        <div className="range-stats">
          <div className="range-stats-title">
            <b>区间统计</b>
            <span>已选择 {rangeRows.length} 根 K 线</span>
            <button onClick={() => { setRangeStart(null); setRangeEnd(null); setRangeMode(true); }}>重新选择</button>
          </div>
          <div className="range-stats-grid">
            <div className="primary"><span>区间涨幅</span><strong className={rangeRise >= 0 ? "up" : "down"}>{formatSignedPct(rangeRise)}</strong></div>
            <div><span>起始日期</span><strong>{rangeFirst.date}</strong></div>
            <div><span>截止日期</span><strong>{rangeLast.date}</strong></div>
            <div><span>统计个数</span><strong>{rangeRows.length}</strong></div>
            <div><span>开盘价</span><strong>{rangeFirst.open.toFixed(2)}</strong></div>
            <div><span>收盘价</span><strong>{rangeLast.close.toFixed(2)}</strong></div>
            <div><span>最高价</span><strong>{rangeHigh.toFixed(2)}</strong></div>
            <div><span>最低价</span><strong>{rangeLow.toFixed(2)}</strong></div>
            <div><span>均价</span><strong>{rangeAverage ? rangeAverage.toFixed(2) : "--"}</strong></div>
            <div><span>成交量</span><strong>{formatAmount(rangeVolume)}手</strong></div>
            <div><span>成交金额</span><strong>{formatAmount(rangeAmount)}</strong></div>
            <div><span>振幅</span><strong>{rangeAmplitude.toFixed(2)}%</strong></div>
            <div><span>最高涨幅</span><strong className={rangeMaxRise >= 0 ? "up" : "down"}>{formatSignedPct(rangeMaxRise)}</strong></div>
            <div><span>最大回撤</span><strong className="down">{rangeMaxDrawdown.toFixed(2)}%</strong></div>
            <div><span>平衡价</span><strong>{rangeBalance.toFixed(2)}</strong></div>
            <div><span>流通盘 / 换手率</span><strong>-- / --</strong></div>
            <div><span>同期上证涨跌幅</span><strong className={(rangeBenchmarks.sh ?? 0) >= 0 ? "up" : "down"}>{rangeBenchmarks.loading ? "计算中…" : rangeBenchmarks.sh === undefined ? "--" : formatSignedPct(rangeBenchmarks.sh)}</strong></div>
            <div><span>同期深证涨跌幅</span><strong className={(rangeBenchmarks.sz ?? 0) >= 0 ? "up" : "down"}>{rangeBenchmarks.loading ? "计算中…" : rangeBenchmarks.sz === undefined ? "--" : formatSignedPct(rangeBenchmarks.sz)}</strong></div>
          </div>
        </div>
      )}
    </div>
  );
}
