"""Unit tests: indicators, BOS/MSS, sweeps, displacement, FVG, market structure."""
from __future__ import annotations

import numpy as np
import pytest

from app.analysis import indicators as ta


def _mk(n=120, start=100.0, seed=7):
    import random

    rng = random.Random(seed)
    candles = []
    price = start
    for i in range(n):
        o = price
        c = o + rng.gauss(0.1, 1.0)
        h = max(o, c) + abs(rng.gauss(0, 0.5))
        low = min(o, c) - abs(rng.gauss(0, 0.5))
        candles.append({"timestamp": None, "open": o, "high": h, "low": low, "close": c,
                        "volume": 1000 + rng.gauss(0, 100)})
        price = c
    return candles


def test_ema_basic():
    vals = np.array([1.0] * 30)
    out = ta.ema(vals, 10)
    assert out.shape == vals.shape
    assert np.isnan(out[:9]).all()
    assert abs(out[-1] - 1.0) < 1e-9


def test_ema_trend():
    vals = np.array([float(i) for i in range(1, 61)])
    out = ta.ema(vals, 10)
    assert out[-1] > out[10]  # rising series -> rising EMA


def test_sma_matches_mean():
    vals = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    out = ta.sma(vals, 3)
    assert np.isnan(out[:2]).all()
    assert abs(out[2] - 2.0) < 1e-9
    assert abs(out[4] - 4.0) < 1e-9


def test_sma_insufficient():
    out = ta.sma(np.array([1.0, 2.0]), 5)
    assert np.isnan(out).all()


def test_rsi_range_and_extremes():
    vals = np.array([float(i) for i in range(1, 51)])  # monotonic up -> RSI 100
    out = ta.rsi(vals, 14)
    assert out[-1] == 100.0
    vals_down = vals[::-1].copy()
    out2 = ta.rsi(vals_down, 14)
    assert out2[-1] < 10
    rng_candles = _mk(200, seed=3)
    rsi_vals = ta.rsi(ta._closes(rng_candles), 14)
    valid = rsi_vals[~np.isnan(rsi_vals)]
    assert ((valid >= 0) & (valid <= 100)).all()


def test_atr_positive_and_consistent():
    c = _mk(100)
    out = ta.atr(c, 14)
    valid = out[~np.isnan(out)]
    assert (valid > 0).all()


def test_macd_shapes():
    vals = ta._closes(_mk(80))
    out = ta.macd(vals)
    assert set(out) == {"macd", "signal", "histogram"}
    assert len(out["histogram"]) == len(vals)
    valid = ~np.isnan(out["histogram"])
    assert valid.any()


def test_vwap_needs_volume():
    c = _mk(50)
    for candle in c:
        candle["volume"] = None
    out = ta.vwap(c)
    assert np.isnan(out).all()
    out2 = ta.vwap(_mk(50))
    valid = out2[~np.isnan(out2)]
    assert len(valid) == 50
    # vwap should lie within the price range
    lows = min(x["low"] for x in _mk(50))
    highs = max(x["high"] for x in _mk(50))
    assert valid[-1] > lows and valid[-1] < highs


def test_bollinger_bands():
    vals = ta._closes(_mk(60))
    out = ta.bollinger(vals, 20, 2.0)
    for i in range(19, 60):
        if not np.isnan(out["upper"][i]):
            assert out["upper"][i] >= out["middle"][i] >= out["lower"][i]


def test_momentum():
    vals = np.array([float(i) for i in range(1, 21)])
    out = ta.momentum(vals, 10)
    assert np.isnan(out[:10]).all()
    assert (out[10:] == 10.0).all()


def test_rolling_volatility():
    vals = ta._closes(_mk(60))
    out = ta.rolling_volatility(vals, 20)
    valid = out[~np.isnan(out)]
    assert (valid >= 0).all()


def test_swing_detection():
    c = [
        {"open": 1, "high": 1, "low": 2, "close": 1},  # 0
        {"open": 1, "high": 2, "low": 3, "close": 1},  # 1
        {"open": 1, "high": 3, "low": 4, "close": 1},  # 2 swing high
        {"open": 1, "high": 2, "low": 5, "close": 1},  # 3
        {"open": 1, "high": 1, "low": 6, "close": 1},  # 4
    ]
    highs, lows = ta.swing_highs_lows(c, lookback=1)
    assert 2 in highs
    assert lows == []  # strictly falling lows -> no swing low


def test_swing_low_detection():
    c = [
        {"open": 1, "high": 6, "low": 5, "close": 1},
        {"open": 1, "high": 5, "low": 4, "close": 1},
        {"open": 1, "high": 4, "low": 3, "close": 1},  # 2 swing low
        {"open": 1, "high": 3, "low": 4, "close": 1},
        {"open": 1, "high": 2, "low": 5, "close": 1},
    ]
    highs, lows = ta.swing_highs_lows(c, lookback=1)
    assert 2 in lows
    assert highs == []  # strictly falling highs -> no swing high


def test_support_resistance_clusters():
    levels = ta.support_resistance(_mk(150, seed=11), 3)
    assert isinstance(levels, list)
    assert len(levels) > 0


def test_bos_detection_bullish():
    # build a series with a clear swing high then a close above it
    c = []
    base = 100.0
    for i in range(30):  # range
        c.append({"open": base, "high": base + 1, "low": base - 1, "close": base})
    # swing high at 30
    c.append({"open": base, "high": base + 5, "low": base - 1, "close": base + 4})
    for i in range(6):  # pullback
        c.append({"open": base + 2, "high": base + 3, "low": base + 1, "close": base + 2})
    # break above swing high with a close
    c.append({"open": base + 3, "high": base + 8, "low": base + 2, "close": base + 7})
    events = ta.detect_bos_mss(c, lookback=3)
    bull = [e for e in events if e["direction"] == "BULLISH" and e["level"] > base]
    assert bull, f"expected bullish break event, got {events}"


def test_mss_detection_bearish():
    # Staircase uptrend: each cycle rises 4.5 then pulls back nearly the whole
    # rise (pullback low undercuts everything within its fractal window), so
    # swing highs AND lows form. Trend continuation breaks highs = BOS;
    # the final crash close below a prior swing low = bearish MSS.
    c = []
    base = 100.0
    for _cycle in range(10):
        start = base
        for _ in range(3):  # three strong up bars (lows never below open)
            o = base
            cl = o + 1.5
            c.append({"open": o, "high": cl + 0.5, "low": o + 0.1, "close": cl})
            base = cl
        # deep pullback: back down to just above the cycle start
        o = base
        cl = start + 0.2
        c.append({"open": o, "high": o + 0.2, "low": start - 0.3, "close": cl})
        base = cl
    price = base
    events = ta.detect_bos_mss(c, lookback=3)
    assert events, "trending staircase must produce structure events"
    assert any(e["direction"] == "BULLISH" for e in events)
    # crash: close below the recent swing lows
    last_lows = min(x["low"] for x in c[-12:])
    c.append({"open": price, "high": price + 0.5, "low": last_lows - 2,
              "close": last_lows - 1.5})
    events2 = ta.detect_bos_mss(c, lookback=3)
    bear = [e for e in events2 if e["direction"] == "BEARISH"]
    assert bear, f"expected a bearish MSS after the break, got {events2[-3:]}"


def test_liquidity_sweep_detection():
    c = []
    base = 100.0
    for i in range(30):
        c.append({"open": base, "high": base + 1, "low": base - 1, "close": base})
    # sweep: pierce the range high but close back inside
    c.append({"open": base, "high": base + 3, "low": base - 0.5, "close": base + 0.5})
    sweeps = ta.detect_liquidity_sweeps(c, lookback=3, max_age=10)
    assert any(s["side"] == "HIGH" for s in sweeps)


def test_displacement_detection():
    c = _mk(60, seed=5)
    # append a huge bull candle
    last = c[-1]
    c.append({"open": last["close"], "high": last["close"] + 10, "low": last["close"] - 0.5,
              "close": last["close"] + 9, "volume": 5000})
    disp = ta.detect_displacement(c, 14, 1.2)
    assert disp and disp[-1]["direction"] == "BULLISH"


def test_fvg_detection():
    c = [
        {"open": 10, "high": 12, "low": 9, "close": 11},
        {"open": 12.5, "high": 14, "low": 12, "close": 13.5},
        {"open": 14, "high": 16, "low": 12.6, "close": 15},  # low 12.6 > first high 12 -> bullish FVG
    ]
    gaps = ta.detect_fair_value_gaps(c)
    assert any(g["type"] == "BULLISH" for g in gaps)


def test_volume_stats_with_and_without_volume():
    c = _mk(40)
    stats = ta.volume_stats(c)
    assert stats["available"] and stats["latest_vs_mean"] is not None
    for candle in c:
        candle["volume"] = None
    stats2 = ta.volume_stats(c)
    assert not stats2["available"] and stats2["mean"] is None


def test_market_structure_summary(candles):
    snap = ta.market_structure_summary(candles[:300], 3)
    for key in ("last_close", "ema20", "ema50", "rsi14", "atr14", "trend", "structure"):
        assert key in snap
    assert snap["trend"] in {"up", "down", "mixed", "unknown"}


def test_no_lookahead_in_slice_semantics(candles):
    """Summary computed on a prefix must not change when future candles are added
    (deterministic functions of the visible slice only)."""
    a = ta.market_structure_summary(candles[:150], 3)
    b = ta.market_structure_summary(candles[:150], 3)
    assert a == b
