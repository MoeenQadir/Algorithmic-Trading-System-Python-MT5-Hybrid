"""Deterministic technical analysis engine (Milestone 6).

Every function is pure Python/NumPy over a list of candle dicts in the
standard normalized schema (ascending by timestamp). No LLM involvement.
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np


def _closes(candles: list[dict[str, Any]]) -> np.ndarray:
    return np.array([float(c["close"]) for c in candles], dtype=float)


def _highs(candles: list[dict[str, Any]]) -> np.ndarray:
    return np.array([float(c["high"]) for c in candles], dtype=float)


def _lows(candles: list[dict[str, Any]]) -> np.ndarray:
    return np.array([float(c["low"]) for c in candles], dtype=float)


def _volumes(candles: list[dict[str, Any]]) -> Optional[np.ndarray]:
    vols = [c.get("volume") for c in candles]
    if any(v is None for v in vols):
        return None
    return np.array([float(v) for v in vols], dtype=float)


# ------------------------------------------------------------------------------
# Basic indicators
# ------------------------------------------------------------------------------
def ema(values: np.ndarray, period: int) -> np.ndarray:
    """Exponential moving average; leading positions before `period` are NaN."""
    out = np.full_like(values, np.nan, dtype=float)
    if len(values) < period or period <= 0:
        return out
    alpha = 2.0 / (period + 1.0)
    out[period - 1] = values[:period].mean()
    for i in range(period, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def sma(values: np.ndarray, period: int) -> np.ndarray:
    out = np.full_like(values, np.nan, dtype=float)
    if len(values) < period or period <= 0:
        return out
    csum = np.cumsum(values)
    out[period - 1:] = (csum[period - 1:] - np.concatenate(([0.0], csum[:-period]))) / period
    return out


def rsi(values: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder's RSI."""
    out = np.full_like(values, np.nan, dtype=float)
    if len(values) < period + 1 or period <= 0:
        return out
    deltas = np.diff(values)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    if avg_loss == 0:
        out[period] = 100.0
    else:
        out[period] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(period + 1, len(values)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def atr(candles: list[dict[str, Any]], period: int = 14) -> np.ndarray:
    """Average True Range (Wilder)."""
    n = len(candles)
    out = np.full(n, np.nan, dtype=float)
    if n < period + 1:
        return out
    highs, lows, closes = _highs(candles), _lows(candles), _closes(candles)
    tr = np.empty(n, dtype=float)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    out[period] = tr[1:period + 1].mean()
    for i in range(period + 1, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def macd(values: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, np.ndarray]:
    """Returns dict with macd, signal, histogram arrays."""
    ema_fast, ema_slow = ema(values, fast), ema(values, slow)
    macd_line = ema_fast - ema_slow
    valid = ~np.isnan(macd_line)
    sig = np.full_like(macd_line, np.nan, dtype=float)
    if valid.sum() >= signal:
        sig[valid] = ema(macd_line[valid], signal)
    hist = macd_line - sig
    return {"macd": macd_line, "signal": sig, "histogram": hist}


def vwap(candles: list[dict[str, Any]]) -> np.ndarray:
    """Session-less rolling VWAP over the provided window. Needs volume."""
    n = len(candles)
    out = np.full(n, np.nan, dtype=float)
    vol = _volumes(candles)
    if vol is None:
        return out
    typical = (_highs(candles) + _lows(candles) + _closes(candles)) / 3.0
    pv = typical * vol
    csum_pv, csum_v = np.cumsum(pv), np.cumsum(vol)
    mask = csum_v > 0
    out[mask] = csum_pv[mask] / csum_v[mask]
    return out


def bollinger(values: np.ndarray, period: int = 20, num_std: float = 2.0) -> dict[str, np.ndarray]:
    mid = sma(values, period)
    out_up = np.full_like(values, np.nan, dtype=float)
    out_lo = np.full_like(values, np.nan, dtype=float)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        sd = window.std(ddof=0)
        out_up[i] = mid[i] + num_std * sd
        out_lo[i] = mid[i] - num_std * sd
    return {"middle": mid, "upper": out_up, "lower": out_lo}


def momentum(values: np.ndarray, period: int = 10) -> np.ndarray:
    out = np.full_like(values, np.nan, dtype=float)
    if len(values) > period:
        out[period:] = values[period:] - values[:-period]
    return out


def rolling_volatility(values: np.ndarray, period: int = 20) -> np.ndarray:
    """Annualization-free rolling std of returns."""
    out = np.full_like(values, np.nan, dtype=float)
    if len(values) < period + 1:
        return out
    rets = np.diff(values) / values[:-1]
    for i in range(period, len(rets) + 1):
        out[i] = rets[i - period:i].std(ddof=0)
    return out


# ------------------------------------------------------------------------------
# Market structure
# ------------------------------------------------------------------------------
def swing_highs_lows(candles: list[dict[str, Any]], lookback: int = 3) -> tuple[list[int], list[int]]:
    """Fractal swing detection. Returns (swing_high_indices, swing_low_indices)."""
    highs, lows = _highs(candles), _lows(candles)
    sw_high, sw_low = [], []
    for i in range(lookback, len(candles) - lookback):
        if highs[i] == max(highs[i - lookback:i + lookback + 1]):
            sw_high.append(i)
        if lows[i] == min(lows[i - lookback:i + lookback + 1]):
            sw_low.append(i)
    return sw_high, sw_low


def support_resistance(candles: list[dict[str, Any]], lookback: int = 3, tolerance_pct: float = 0.0015) -> list[float]:
    """Cluster swing levels into support/resistance zones."""
    sw_high, sw_low = swing_highs_lows(candles, lookback)
    levels = sorted([_highs(candles)[i] for i in sw_high] + [_lows(candles)[i] for i in sw_low])
    clusters: list[list[float]] = []
    for lvl in levels:
        if clusters and abs(lvl - np.mean(clusters[-1])) / np.mean(clusters[-1]) <= tolerance_pct:
            clusters[-1].append(lvl)
        else:
            clusters.append([lvl])
    return [round(float(np.mean(c)), 10) for c in clusters]


def detect_bos_mss(candles: list[dict[str, Any]], lookback: int = 3) -> list[dict[str, Any]]:
    """Deterministic structure breaks based on the most recent confirmed range."""
    events: list[dict[str, Any]] = []
    if len(candles) < 2:
        return events

    highs = _highs(candles)
    lows = _lows(candles)
    closes = _closes(candles)
    trend_state: Optional[str] = None
    last_bullish_level: Optional[float] = None
    last_bearish_level: Optional[float] = None

    for i in range(1, len(candles)):
        start = max(0, i - lookback)
        prev_high = float(np.max(highs[start:i])) if i > start else float(highs[0])
        prev_low = float(np.min(lows[start:i])) if i > start else float(lows[0])

        if closes[i] > prev_high and closes[i - 1] <= prev_high:
            etype = "BOS" if trend_state == "BULLISH" else "MSS"
            events.append({"index": i, "type": etype, "direction": "BULLISH", "level": prev_high})
            trend_state = "BULLISH"
            last_bullish_level = prev_high

        if closes[i] < prev_low and closes[i - 1] >= prev_low:
            etype = "BOS" if trend_state == "BEARISH" else "MSS"
            events.append({"index": i, "type": etype, "direction": "BEARISH", "level": prev_low})
            trend_state = "BEARISH"
            last_bearish_level = prev_low

    return events


def detect_liquidity_sweeps(candles: list[dict[str, Any]], lookback: int = 3, max_age: int = 20,
                            start_index: int = 0) -> list[dict[str, Any]]:
    """A sweep: wick takes out a prior extreme but the close returns inside
    (stop-hunt / liquidity grab).

    Extremes are computed over a trailing window [i - max_age, i - 1]. This is
    look-ahead-free and also catches sweeps of levels that are not yet
    confirmed fractal swings. `start_index` limits scanning (performance only;
    results for those bars are identical to a full-series scan)."""
    sweeps: list[dict[str, Any]] = []
    highs, lows, closes = _highs(candles), _lows(candles), _closes(candles)
    n = len(candles)
    for i in range(max(start_index, max_age + 1), n):
        prior_hi = float(np.max(highs[i - max_age:i]))
        prior_lo = float(np.min(lows[i - max_age:i]))
        prior_hi_idx = i - max_age + int(np.argmax(highs[i - max_age:i]))
        prior_lo_idx = i - max_age + int(np.argmin(lows[i - max_age:i]))
        if highs[i] > prior_hi and closes[i] < prior_hi:
            sweeps.append({"index": i, "side": "HIGH", "level": prior_hi, "swept_index": prior_hi_idx})
        elif lows[i] < prior_lo and closes[i] > prior_lo:
            sweeps.append({"index": i, "side": "LOW", "level": prior_lo, "swept_index": prior_lo_idx})
    return sweeps


def detect_displacement(candles: list[dict[str, Any]], atr_period: int = 14, multiplier: float = 1.2) -> list[dict[str, Any]]:
    """Displacement: a candle whose body exceeds ATR * multiplier and closes
    directionally — indicates aggressive institutional order flow."""
    body = np.abs(_closes(candles) - np.array([float(c["open"]) for c in candles], dtype=float))
    atrs = atr(candles, atr_period)
    out: list[dict[str, Any]] = []
    for i, c in enumerate(candles):
        if np.isnan(atrs[i]) or atrs[i] == 0:
            continue
        if body[i] >= multiplier * atrs[i]:
            direction = "BULLISH" if c["close"] >= c["open"] else "BEARISH"
            out.append({"index": i, "direction": direction, "body": float(body[i]), "atr": float(atrs[i])})
    return out


def detect_fair_value_gaps(candles: list[dict[str, Any]], min_size_atr: float = 0.0) -> list[dict[str, Any]]:
    """3-candle imbalance: gap between candle[0].high and candle[2].low (bullish)
    or between candle[0].low and candle[2].high (bearish)."""
    if min_size_atr > 0:
        atrs = atr(candles, 14)
    else:
        atrs = np.full(len(candles), np.nan)
    gaps: list[dict[str, Any]] = []
    for i in range(2, len(candles)):
        a, _c, b = candles[i - 2], candles[i - 1], candles[i]
        if b["low"] > a["high"]:
            size = float(b["low"] - a["high"])
            if min_size_atr <= 0 or (not np.isnan(atrs[i]) and size >= min_size_atr * atrs[i]):
                gaps.append({"index": i, "type": "BULLISH", "top": float(b["low"]), "bottom": float(a["high"]), "size": size})
        if b["high"] < a["low"]:
            size = float(a["low"] - b["high"])
            if min_size_atr <= 0 or (not np.isnan(atrs[i]) and size >= min_size_atr * atrs[i]):
                gaps.append({"index": i, "type": "BEARISH", "top": float(a["low"]), "bottom": float(b["high"]), "size": size})
    return gaps


def volume_stats(candles: list[dict[str, Any]]) -> dict[str, Optional[float]]:
    vol = _volumes(candles)
    if vol is None or len(vol) == 0:
        return {"available": False, "mean": None, "latest": None, "latest_vs_mean": None}
    mean = float(vol.mean())
    latest = float(vol[-1])
    return {
        "available": True,
        "mean": mean,
        "latest": latest,
        "latest_vs_mean": (latest / mean) if mean > 0 else None,
    }


def market_structure_summary(candles: list[dict[str, Any]], swing_lookback: int = 3) -> dict[str, Any]:
    """Compact deterministic technical snapshot used by agents."""
    closes = _closes(candles)
    ema20, ema50 = ema(closes, 20), ema(closes, 50)
    rsi14 = rsi(closes, 14)
    atr14 = atr(candles, 14)
    macd_out = macd(closes)
    bb = bollinger(closes, 20, 2.0)

    def last(arr: np.ndarray) -> Optional[float]:
        v = arr[-1] if len(arr) else np.nan
        return None if v is None or np.isnan(v) else float(v)

    bos_mss = detect_bos_mss(candles, swing_lookback)
    last_event = bos_mss[-1] if bos_mss else None
    structure = "unknown"
    if last_event:
        if last_event["direction"] == "BULLISH":
            structure = "bullish_bos" if last_event["type"] == "BOS" else "bullish_mss"
        else:
            structure = "bearish_bos" if last_event["type"] == "BOS" else "bearish_mss"

    trend = "unknown"
    e20, e50 = last(ema20), last(ema50)
    last_close = float(closes[-1])
    if e20 is not None and e50 is not None:
        if e20 > e50 and last_close > e20:
            trend = "up"
        elif e20 < e50 and last_close < e20:
            trend = "down"
        else:
            trend = "mixed"

    return {
        "last_close": last_close,
        "ema20": e20,
        "ema50": e50,
        "rsi14": last(rsi14),
        "atr14": last(atr14),
        "atr_pct": (last(atr14) / last_close) if last(atr14) and last_close else None,
        "macd_histogram": last(macd_out["histogram"]),
        "bollinger_upper": last(bb["upper"]),
        "bollinger_lower": last(bb["lower"]),
        "trend": trend,
        "structure": structure,
        "support_resistance": support_resistance(candles, swing_lookback),
        "recent_bos_mss": bos_mss[-3:],
        "recent_sweeps": detect_liquidity_sweeps(candles, swing_lookback)[-3:],
        "recent_displacement": detect_displacement(candles)[-3:],
        "fair_value_gaps": detect_fair_value_gaps(candles)[-3:],
        "volume": volume_stats(candles),
        "candle_count": len(candles),
    }
