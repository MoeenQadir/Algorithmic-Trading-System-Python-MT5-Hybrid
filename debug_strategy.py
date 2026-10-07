from app.analysis.strategy import evaluate_setup, DEFAULT_PARAMS
from app.analysis import indicators as ta
from datetime import datetime, timedelta
from app.utils.timeutils import utcnow


def make_long_setup_candles(n=200):
    """Generate candles with sweep of LOWS at n-4.
    
    Strategy:
    - Range around 100 with highs ~100.2, lows ~99.8
    - Bar n-4: low breaks below 99.8 (prior low) but high stays under 100.2, close back above 99.8
    - Bar n-3: bullish BOS breaking above 100.2
    - Bar n-2: displacement candle (large bullish body)
    - Bar n-1: continue up
    """
    candles = []
    
    for i in range(n):
        if i < n - 4:
            # Range-bound: oscillations between 99.8 and 100.2
            o = 100.0
            c = 100.0 + (0.4 if i % 2 == 0 else -0.4)
            high = max(o, c) + 0.1
            low = min(o, c) - 0.1
        elif i == n - 4:
            # Sweep of LOWS: low goes below 99.8, high stays at/below 100.2, close back above 99.8
            o, c, high, low = 100.0, 100.1, 100.2, 98.5
        elif i == n - 3:
            # Bullish BOS: close breaks above 100.2
            o, c, high, low = 100.1, 100.5, 100.6, 99.9
        elif i == n - 2:
            # Displacement: large bullish candle
            o, c, high, low = 100.5, 103.5, 103.7, 100.3
        else:
            # Last bar: continue up
            o, c, high, low = 103.5, 104.5, 104.7, 103.3
        
        candles.append({
            "timestamp": None,
            "open": round(o, 4),
            "high": round(high, 4),
            "low": round(low, 4),
            "close": round(c, 4),
            "volume": 1000.0,
        })
    
    return candles


def make_short_setup_candles(n=200):
    """Generate candles with sweep of HIGHS at n-4 for a SHORT setup."""
    candles = []
    
    for i in range(n):
        if i < n - 4:
            # Range-bound: oscillations between 99.8 and 100.2
            o = 100.0
            c = 100.0 + (0.4 if i % 2 == 0 else -0.4)
            high = max(o, c) + 0.1
            low = min(o, c) - 0.1
        elif i == n - 4:
            # Sweep of HIGHS: high goes above 100.2, low stays at/above 99.8, close back below 100.2
            o, c, high, low = 100.0, 99.9, 101.5, 99.8
        elif i == n - 3:
            # Bearish BOS: close breaks below 99.8
            o, c, high, low = 99.9, 99.5, 100.1, 99.4
        elif i == n - 2:
            # Displacement: large bearish candle
            o, c, high, low = 99.5, 96.5, 99.7, 96.3
        else:
            o, c, high, low = 96.5, 95.5, 96.7, 95.3
        
        candles.append({
            "timestamp": None,
            "open": round(o, 4),
            "high": round(high, 4),
            "low": round(low, 4),
            "close": round(c, 4),
            "volume": 1000.0,
        })
    
    return candles


# Test LONG setup
candles = make_long_setup_candles()
start = utcnow() - timedelta(minutes=15 * len(candles))
for i, c in enumerate(candles):
    c["timestamp"] = start + timedelta(minutes=15 * i)

p = DEFAULT_PARAMS
n = len(candles)
atr_arr = ta.atr(candles, 14)
atr_val = float(atr_arr[-1])
last_close = float(candles[-1]["close"])

print(f"=== LONG SETUP ===")
print(f"ATR%: {atr_val / last_close:.4f} (max: {p['max_atr_pct']})")

start_idx = max(0, n - 1 - p["confirmation_lookback"] - p["sweep_max_age"])
sweeps = ta.detect_liquidity_sweeps(candles, p["swing_lookback"], p["sweep_max_age"], start_index=start_idx)
cutoff = n - 1 - p["confirmation_lookback"]
recent = [s for s in sweeps if cutoff <= s["index"] <= n - 1]
print(f"Recent sweeps (cutoff={cutoff}): {recent}")

bos_mss = ta.detect_bos_mss(candles, p["swing_lookback"])
print(f"BOS/MSS near end:")
for e in bos_mss:
    if e['index'] >= n - 10:
        print(f"  {e['type']} {e['direction']} at bar {e['index']}, level={e['level']:.2f}")

disp = ta.detect_displacement(candles, 14, p["displacement_atr"])
print(f"Displacements near end:")
for d in disp:
    if d['index'] >= n - 10:
        print(f"  {d['direction']} at bar {d['index']}, body={d['body']:.2f}")

sig = evaluate_setup(candles, {}, "XAUUSD", "15m")
print(f"\nSignal: direction={sig.direction}, valid={sig.setup_valid}")
print(f"Entry: {sig.entry}, SL: {sig.stop_loss}, TP: {sig.take_profit}")
print(f"RR: {sig.risk_reward}, Confidence: {sig.confidence}")
print(f"Reasons: {sig.reasons}")
print(f"Invalidations: {sig.invalidations}")

# Test SHORT setup
print(f"\n=== SHORT SETUP ===")
candles = make_short_setup_candles()
start = utcnow() - timedelta(minutes=15 * len(candles))
for i, c in enumerate(candles):
    c["timestamp"] = start + timedelta(minutes=15 * i)

atr_arr = ta.atr(candles, 14)
atr_val = float(atr_arr[-1])
last_close = float(candles[-1]["close"])
print(f"ATR%: {atr_val / last_close:.4f}")

sweeps = ta.detect_liquidity_sweeps(candles, p["swing_lookback"], p["sweep_max_age"], start_index=start_idx)
recent = [s for s in sweeps if cutoff <= s["index"] <= n - 1]
print(f"Recent sweeps: {recent}")

bos_mss = ta.detect_bos_mss(candles, p["swing_lookback"])
for e in bos_mss:
    if e['index'] >= n - 10:
        print(f"  {e['type']} {e['direction']} at bar {e['index']}")

disp = ta.detect_displacement(candles, 14, p["displacement_atr"])
for d in disp:
    if d['index'] >= n - 10:
        print(f"  {d['direction']} at bar {d['index']}, body={d['body']:.2f}")

sig = evaluate_setup(candles, {}, "XAUUSD", "15m")
print(f"\nSignal: direction={sig.direction}, valid={sig.setup_valid}")
print(f"Entry: {sig.entry}, SL: {sig.stop_loss}, TP: {sig.take_profit}")
print(f"RR: {sig.risk_reward}, Confidence: {sig.confidence}")
print(f"Reasons: {sig.reasons}")