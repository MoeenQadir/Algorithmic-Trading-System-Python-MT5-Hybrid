"""Strategy engine (Milestone 7).

Configurable institutional-style strategy:
liquidity sweep -> MSS/BOS -> displacement -> supply/demand entry zone ->
confirmation -> entry / stop / target.

All thresholds are parameters, never hard-coded in the logic.
Deterministic: the same candles + params always produce the same signal.
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np

from app.analysis import indicators as ta
from app.analysis.schemas import StrategySignal
from app.utils.timeutils import utcnow

DEFAULT_PARAMS: dict[str, Any] = {
    "swing_lookback": 3,
    "sweep_max_age": 20,
    "displacement_required": True,
    "displacement_atr": 1.2,
    "stop_buffer_atr": 0.25,
    "rr_target": 2.0,
    "max_atr_pct": 0.02,
    "confirmation_lookback": 3,
    "risk_reward_floor": 2.0,
    "entry_mode": "close",
    "max_stop_atr": 5.0,
}


def validate_params(params: dict[str, Any]) -> dict[str, Any]:
    """Merge user params over defaults with type/range validation."""
    merged = dict(DEFAULT_PARAMS)
    for key, value in (params or {}).items():
        if key in merged and value is not None:
            merged[key] = value
    merged["swing_lookback"] = max(1, int(merged["swing_lookback"]))
    merged["sweep_max_age"] = max(2, int(merged["sweep_max_age"]))
    merged["displacement_atr"] = max(0.1, float(merged["displacement_atr"]))
    merged["stop_buffer_atr"] = max(0.0, float(merged["stop_buffer_atr"]))
    merged["rr_target"] = max(1.0, float(merged["rr_target"]))
    merged["risk_reward_floor"] = max(1.0, float(merged["risk_reward_floor"]))
    merged["max_atr_pct"] = max(0.0001, float(merged["max_atr_pct"]))
    merged["max_stop_atr"] = max(1.0, float(merged["max_stop_atr"]))
    return merged


def _invalid(symbol: str, timeframe: str, direction: str, confidence: float,
            reasons: list[str], invalidations: list[str]) -> StrategySignal:
    return StrategySignal(
        symbol=symbol, timeframe=timeframe, direction=direction, setup_valid=False,
        confidence=confidence, reasons=reasons, invalidations=invalidations, timestamp=utcnow(),
    )


def evaluate_setup(
    candles: list[dict[str, Any]],
    params: dict[str, Any] | None = None,
    symbol: str = "",
    timeframe: str = "",
) -> StrategySignal:
    """Evaluate the configured setup on a candle series (ascending order)."""
    p = validate_params(params or {})
    reasons: list[str] = []
    invalidations: list[str] = []
    n = len(candles)
    min_bars = max(p["swing_lookback"] * 2 + 2, 30)
    if n < min_bars:
        return _invalid(symbol, timeframe, "NONE", 0.0, [f"insufficient candles ({n} < {min_bars})"], [])

    atr_arr = ta.atr(candles, 14)
    atr_val = float(atr_arr[-1]) if not np.isnan(atr_arr[-1]) else None
    if atr_val is None or atr_val <= 0:
        return _invalid(symbol, timeframe, "NONE", 0.0, ["ATR unavailable"], [])

    last_close = float(candles[-1]["close"])
    if last_close <= 0:
        return _invalid(symbol, timeframe, "NONE", 0.0, ["invalid prices"], [])
    if (atr_val / last_close) > p["max_atr_pct"]:
        return _invalid(symbol, timeframe, "NONE", 0.0, ["volatility filter: ATR% above max_atr_pct"], [])

    # 1) liquidity sweep within the confirmation window (scan only recent bars:
    #    deterministic and equivalent to a full scan for those bars)
    start = max(0, n - 1 - p["confirmation_lookback"] - p["sweep_max_age"])
    sweeps = ta.detect_liquidity_sweeps(candles, p["swing_lookback"], p["sweep_max_age"],
                                        start_index=start)
    cutoff = n - 1 - p["confirmation_lookback"]
    recent = [s for s in sweeps if cutoff <= s["index"] <= n - 1]
    if not recent:
        return _invalid(symbol, timeframe, "NONE", 0.0, ["no recent liquidity sweep"], [])
    sweep = recent[-1]
    reasons.append(f"liquidity sweep of {'highs' if sweep['side'] == 'HIGH' else 'lows'} @ {sweep['level']:.4f}")

    direction = "LONG" if sweep["side"] == "LOW" else "SHORT"
    invalidations.append(
        f"sweep invalidated if price closes {'below' if direction == 'LONG' else 'above'} {sweep['level']:.4f} against the setup"
    )

    # 2) MSS/BOS confirmation in the sweep direction after the sweep bar
    bos_mss = ta.detect_bos_mss(candles, p["swing_lookback"])
    wanted = "BULLISH" if direction == "LONG" else "BEARISH"
    confirm = [e for e in bos_mss if e["index"] > sweep["index"] and e["direction"] == wanted]
    if not confirm:
        return _invalid(symbol, timeframe, direction, 0.1, reasons + ["no MSS/BOS confirmation after sweep"], invalidations)
    confirm_event = confirm[-1]
    reasons.append(
        f"{confirm_event['type']} {'bullish' if confirm_event['direction'] == 'BULLISH' else 'bearish'} "
        f"confirmation at bar {confirm_event['index']}"
    )

    # 3) displacement after the sweep
    disp = [d for d in ta.detect_displacement(candles, 14, p["displacement_atr"]) if d["index"] > sweep["index"]]
    if p["displacement_required"] and not disp:
        return _invalid(symbol, timeframe, direction, 0.15, reasons + ["no displacement candle after sweep"], invalidations)
    if disp:
        last_disp = disp[-1]
        reasons.append(
            f"displacement ({last_disp['direction'].lower()}) body {last_disp['body']:.4f} >= "
            f"{p['displacement_atr']}xATR"
        )

    # 4) entry sanity: entry is the last close; it must be on the correct side
    #    of the swept level and the stop distance must not be absurd
    entry = last_close
    swept_level = float(sweep["level"])
    if direction == "LONG" and entry <= swept_level:
        return _invalid(symbol, timeframe, direction, 0.2, reasons + ["entry not above swept level"], invalidations)
    if direction == "SHORT" and entry >= swept_level:
        return _invalid(symbol, timeframe, direction, 0.2, reasons + ["entry not below swept level"], invalidations)

    # 5) stop beyond the sweep wick with ATR buffer; target at rr_target R
    sweep_bar = candles[sweep["index"]]
    if direction == "LONG":
        stop = min(float(sweep_bar["low"]), swept_level) - p["stop_buffer_atr"] * atr_val
        target = entry + (entry - stop) * p["rr_target"]
    else:
        stop = max(float(sweep_bar["high"]), swept_level) + p["stop_buffer_atr"] * atr_val
        target = entry - (stop - entry) * p["rr_target"]

    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return _invalid(symbol, timeframe, direction, 0.2, reasons + ["zero stop distance"], invalidations)
    stop_limit = p["max_stop_atr"] * atr_val * 3.0
    if stop_dist > stop_limit:
        return _invalid(symbol, timeframe, direction, 0.2,
                        reasons + [f"stop distance {stop_dist:.4f} exceeds {stop_limit:.4f} (configurable max stop threshold)"],
                        invalidations)
    risk_reward = abs(target - entry) / stop_dist
    if risk_reward < p["risk_reward_floor"]:
        return _invalid(symbol, timeframe, direction, 0.2,
                        reasons + [f"RR {risk_reward:.2f} below floor {p['risk_reward_floor']}"], invalidations)

    invalidations.extend([
        f"setup invalid if price closes {'below' if direction == 'LONG' else 'above'} {stop:.4f}",
        "setup invalid if a new opposing liquidity sweep forms before entry",
    ])
    confidence = 0.55 + 0.1 * min(len(disp), 2) + (0.1 if confirm_event["type"] == "MSS" else 0.05)
    return StrategySignal(
        symbol=symbol, timeframe=timeframe, direction=direction, setup_valid=True,
        entry=round(entry, 10), stop_loss=round(stop, 10), take_profit=round(target, 10),
        risk_reward=round(risk_reward, 2), confidence=round(min(0.9, confidence), 2),
        reasons=reasons, invalidations=invalidations, timestamp=utcnow(),
    )
