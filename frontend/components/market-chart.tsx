"use client";

import { useEffect, useRef, useState } from "react";
import {
  CandlestickSeries,
  ColorType,
  createChart,
  HistogramSeries,
  LineSeries,
  type UTCTimestamp,
} from "lightweight-charts";

export type CandlePoint = {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
};

type EquityPoint = [string, number];

type Props = {
  candles?: CandlePoint[];
  equity?: EquityPoint[];
  kind?: "candles" | "equity";
  indicators?: {
    sma20?: number[];
    sma50?: number[];
    vwap?: number[];
  };
  showVolume?: boolean;
};

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function toChartTime(candle: CandlePoint): UTCTimestamp {
  return Math.floor(new Date(candle.timestamp).getTime() / 1000) as UTCTimestamp;
}

function calculateSMA(data: number[], period: number): number[] {
  const result: number[] = [];
  for (let i = 0; i < data.length; i++) {
    if (i < period - 1) {
      result.push(NaN);
    } else {
      let sum = 0;
      for (let j = i - period + 1; j <= i; j++) {
        sum += data[j];
      }
      result.push(sum / period);
    }
  }
  return result;
}

function calculateVWAP(candles: CandlePoint[]): number[] {
  const result: number[] = [];
  let cumulativeVolume = 0;
  let cumulativePV = 0;
  for (const candle of candles) {
    const typicalPrice = (candle.high + candle.low + candle.close) / 3;
    const volume = candle.volume || 1;
    cumulativePV += typicalPrice * volume;
    cumulativeVolume += volume;
    result.push(cumulativePV / cumulativeVolume);
  }
  return result;
}

function resolveThemeColors(): Record<string, string> {
  const cs = getComputedStyle(document.documentElement);
  const get = (name: string, fallback: string) => {
    const v = cs.getPropertyValue(name).trim();
    return v || fallback;
  };
  return {
    panel: get("--panel", "#ffffff"),
    text: get("--text", "#0b1211"),
    muted: get("--muted", "#5a7066"),
    line: get("--line", "#e0e8e4"),
    green: get("--green", "#1a7f4a"),
    red: get("--red", "#c0392b"),
  };
}

export function MarketChart({
  candles = [],
  equity = [],
  kind = "candles",
  indicators = {},
  showVolume = true
}: Props) {
  const host = useRef<HTMLDivElement>(null);
  const [themeKey, setThemeKey] = useState(0);

  useEffect(() => {
    const observer = new MutationObserver(() => setThemeKey((k) => k + 1));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!host.current) return;
    const theme = resolveThemeColors();
    const chart = createChart(host.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: theme.panel },
        textColor: theme.muted,
        fontFamily: "Bahnschrift, Segoe UI, sans-serif"
      },
      grid: { vertLines: { color: theme.line }, horzLines: { color: theme.line } },
      rightPriceScale: { borderColor: theme.line },
      timeScale: { borderColor: theme.line, timeVisible: true, secondsVisible: false },
      crosshair: { vertLine: { color: theme.green }, horzLine: { color: theme.green } },
    });

    if (kind === "equity") {
      const series = chart.addSeries(LineSeries, {
        color: theme.green,
        lineWidth: 2,
        crosshairMarkerRadius: 3
      });
const uniqueEquity = equity
        .filter(([, value]) => isFiniteNumber(value))
        .sort(([a], [b]) => new Date(a).getTime() - new Date(b).getTime())
        .reduce((acc, [timestamp, value]) => {
          const timeKey = Math.floor(new Date(timestamp).getTime() / 1000);
          if (!acc.some(item => item.time === timeKey)) {
            acc.push({ time: timeKey as UTCTimestamp, value: value as number });
          }
          return acc;
        }, [] as { time: UTCTimestamp; value: number }[]);

      series.setData(uniqueEquity);
     } else {
       // Process candles: sort by time and remove duplicates
       const processedCandles = candles
         .filter((c) => isFiniteNumber(c.open) && isFiniteNumber(c.high) && isFiniteNumber(c.low) && isFiniteNumber(c.close))
         .map((candle) => ({
           time: toChartTime(candle),
           open: candle.open,
           high: candle.high,
           low: candle.low,
           close: candle.close,
         }))
         .sort((a, b) => a.time - b.time)
         .filter((candle, index, array) => 
           index === 0 || candle.time > array[index - 1].time
         );

       const series = chart.addSeries(CandlestickSeries, {
         upColor: theme.green,
         downColor: theme.red,
         borderVisible: false,
         wickUpColor: theme.green,
         wickDownColor: theme.red,
       });
       series.setData(processedCandles);

       // Use processed candles for indicators to ensure consistent ordering
       const closePrices = processedCandles.map(c => c.close);

       if (!indicators.sma20 && closePrices.length >= 20) {
         const sma20 = calculateSMA(closePrices, 20);
         const sma20Series = chart.addSeries(LineSeries, {
           color: "#2196f3",
           lineWidth: 2,
           lineStyle: 2,
           crosshairMarkerVisible: false
         });
         sma20Series.setData(processedCandles
           .map((candle, i) => ({ time: candle.time, value: sma20[i] }))
           .filter((point) => isFiniteNumber(point.value)));
       }

       if (!indicators.sma50 && closePrices.length >= 50) {
         const sma50 = calculateSMA(closePrices, 50);
         const sma50Series = chart.addSeries(LineSeries, {
           color: "#ff9800",
           lineWidth: 2,
           lineStyle: 2,
           crosshairMarkerVisible: false
         });
         sma50Series.setData(processedCandles
           .map((candle, i) => ({ time: candle.time, value: sma50[i] }))
           .filter((point) => isFiniteNumber(point.value)));
       }

       if (!indicators.vwap) {
         const vwap = calculateVWAP(processedCandles);
         const vwapSeries = chart.addSeries(LineSeries, {
           color: "#9c27b0",
           lineWidth: 2,
           lineStyle: 3,
           crosshairMarkerVisible: false
         });
         vwapSeries.setData(processedCandles
           .map((candle, i) => ({ time: candle.time, value: vwap[i] }))
           .filter((point) => isFiniteNumber(point.value)));
       }

       if (showVolume && processedCandles.some(c => c.volume !== undefined && c.volume !== null)) {
         const volumeSeries = chart.addSeries(HistogramSeries, {
           color: theme.green,
           priceFormat: { type: "volume" },
           priceScaleId: "",
         });
         volumeSeries.setData(processedCandles
           .map((candle) => ({
             time: candle.time,
             value: candle.volume || 0,
             color: candle.close >= candle.open ? theme.green : theme.red,
           }))
           .filter((point) => isFiniteNumber(point.value)));
       }
     }

    chart.timeScale().fitContent();
    return () => {
      chart.remove();
    };
  }, [candles, equity, kind, indicators, showVolume]);

  return (
    <div
      className="chart-host"
      ref={host}
      key={themeKey}
      aria-label={kind === "equity" ? "Portfolio equity chart" : "Market candlestick chart"}
    />
  );
}
