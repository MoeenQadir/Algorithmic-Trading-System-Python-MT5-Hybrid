"""Tests: strategy engine, position sizing, risk engine, decision engine."""
from __future__ import annotations

import numpy as np
import pytest

from app.analysis.strategy import DEFAULT_PARAMS, evaluate_setup, validate_params
from app.risk.engine import compute_position_size


def _range_with_sweep_setup(direction="up", seed=9):
    """Build: 60-bar quiet range (defines liquidity) -> sweep of the range
    extreme -> displacement -> confirmation. Ends with a valid setup."""
    import random

    rng = random.Random(seed)
    candles = []
    price = 100.0
    top, bottom = 101.0, 99.0
    for i in range(60):
        o = price
        c = top - 0.1 if o >= top - 0.5 else (bottom + 0.1 if o <= bottom + 0.5 else o + rng.gauss(0, 0.1))
        c = min(max(c, bottom + 0.05), top - 0.05)
        candles.append({"timestamp": None, "open": o, "high": max(o, c) + 0.03,
                        "low": min(o, c) - 0.03, "close": c, "volume": 1000.0})
        price = c
    # sweep bar: pierce the range low (longs) or high (shorts), close back inside
    last_close = candles[-1]["close"]
    if direction == "up":
        candles.append({"timestamp": None, "open": last_close, "high": last_close + 0.3,
                        "low": bottom - 0.8, "close": last_close + 0.2, "volume": 1500.0})
    else:
        candles.append({"timestamp": None, "open": last_close, "high": top + 0.8,
                        "low": last_close - 0.3, "close": last_close - 0.2, "volume": 1500.0})
    # displacement + continuation bars in the sweep-reversal direction
    last_close = candles[-1]["close"]
    step = 1.2 if direction == "up" else -1.2
    for i in range(3):
        o = last_close
        c = o + step
        candles.append({"timestamp": None, "open": o, "high": max(o, c) + 0.2,
                        "low": min(o, c) - 0.2, "close": c, "volume": 2200.0})
        last_close = c
    return candles


def test_validate_params_defaults():
    p = validate_params({})
    assert p["swing_lookback"] == DEFAULT_PARAMS["swing_lookback"]
    assert p["rr_target"] >= 1.0


def test_validate_params_rejects_bad_types():
    with pytest.raises(Exception):
        validate_params({"swing_lookback": "not-a-number"})


def test_strategy_insufficient_data():
    sig = evaluate_setup([{"open": 1, "high": 1, "low": 1, "close": 1}] * 10, {}, "US100", "15m")
    assert not sig.setup_valid
    assert sig.direction == "NONE"
    assert any("insufficient" in r for r in sig.reasons)


def test_strategy_no_setup_in_quiet_range():
    import random

    rng = random.Random(5)
    candles = []
    price = 100.0
    for i in range(120):
        o = price
        c = o + rng.gauss(0, 0.1)
        candles.append({"timestamp": None, "open": o, "high": max(o, c) + 0.05,
                        "low": min(o, c) - 0.05, "close": c, "volume": 1000.0})
        price = c
    sig = evaluate_setup(candles, {}, "US100", "15m")
    assert not sig.setup_valid


def test_strategy_finds_engineered_setup():
    candles = _range_with_sweep_setup("up")
    params = {**DEFAULT_PARAMS, "max_atr_pct": 0.05}
    sig = evaluate_setup(candles, params, "US100", "15m")
    assert sig.direction == "LONG"
    assert sig.setup_valid is True
    assert sig.entry is not None and sig.stop_loss is not None and sig.take_profit is not None
    assert sig.stop_loss < sig.entry < sig.take_profit
    assert sig.risk_reward >= 1.99


def test_strategy_short_setup():
    candles = _range_with_sweep_setup("down")
    params = {**DEFAULT_PARAMS, "max_atr_pct": 0.05}
    sig = evaluate_setup(candles, params, "US100", "15m")
    assert sig.direction == "SHORT"
    assert sig.setup_valid is True
    assert sig.take_profit < sig.entry < sig.stop_loss
    assert sig.risk_reward >= 1.99


def test_position_size_long():
    out = compute_position_size(10000.0, 0.004, 100.0, 98.0, "LONG")
    assert out["risk_amount"] == pytest.approx(40.0)
    assert out["quantity"] == pytest.approx(20.0)
    assert out["notional"] == pytest.approx(2000.0)
    assert out["max_loss"] == pytest.approx(40.0)


def test_position_size_short():
    out = compute_position_size(10000.0, 0.004, 98.0, 100.0, "SHORT")
    assert out["quantity"] == pytest.approx(20.0)


def test_position_size_zero_stop_distance():
    out = compute_position_size(10000.0, 0.004, 100.0, 100.0, "LONG")
    assert out["quantity"] is None


def test_position_size_negative_values():
    out = compute_position_size(-5000, 0.004, 100.0, 98.0, "LONG")
    assert out["quantity"] is None
    out2 = compute_position_size(10000.0, -0.01, 100.0, 98.0, "LONG")
    assert out2["quantity"] is None


def test_position_size_invalid_prices():
    assert compute_position_size(10000.0, 0.004, 0, 98.0, "LONG")["quantity"] is None
    assert compute_position_size(10000.0, 0.004, None, 98.0, "LONG")["quantity"] is None
    assert compute_position_size(10000.0, 0.004, "abc", 98.0, "LONG")["quantity"] is None


def test_position_size_very_large_and_small():
    big = compute_position_size(10_000_000.0, 0.01, 100.0, 99.0, "LONG")
    assert big["quantity"] == pytest.approx(100_000.0)
    tiny = compute_position_size(100.0, 0.0001, 100.0, 99.0, "LONG")
    assert 0 < tiny["quantity"] < 1


def test_position_size_contract_multiplier():
    out = compute_position_size(100000.0, 0.004, 15000.0, 14950.0, "LONG", contract_multiplier=5.0)
    assert out["risk_amount"] == pytest.approx(400.0)
    assert out["quantity"] == pytest.approx(400.0 / 250.0)
    assert out["notional"] == pytest.approx(out["quantity"] * 15000.0 * 5.0)


# ------------------------------------------------------------------------------
# Decision engine
# ------------------------------------------------------------------------------
def test_decision_wait_without_candidate():
    from app.decision.engine import DecisionEngine

    agents = {
        "technical": {"direction": "LONG", "confidence": 0.8},
        "quant": {"direction": "LONG", "confidence": 0.7},
        "strategy": {"candidate_trade": False, "direction": "NONE", "confidence": 0.1},
        "regime": {"regime": "TRENDING", "confidence": 0.6},
    }
    out = DecisionEngine().decide(agents, "US100", "15m")
    assert out.action == "WAIT"


def test_decision_buy_with_aligned_evidence():
    from app.decision.engine import DecisionEngine

    agents = {
        "technical": {"direction": "LONG", "confidence": 0.8},
        "quant": {"direction": "LONG", "confidence": 0.7},
        "strategy": {"candidate_trade": True, "direction": "LONG", "confidence": 0.8,
                     "entry": 100.0, "stop": 99.0, "target": 102.0,
                     "reasons": [], "conflicts": []},
        "regime": {"regime": "TRENDING", "confidence": 0.6},
        "news": {"status": "unavailable"},
        "sentiment": {"status": "unavailable"},
    }
    out = DecisionEngine().decide(agents, "US100", "15m")
    assert out.action == "BUY"
    assert out.direction == "LONG"
    assert out.risk_reward == pytest.approx(2.0)


def test_decision_reject_low_rr():
    from app.decision.engine import DecisionEngine

    agents = {
        "technical": {"direction": "SHORT", "confidence": 0.8},
        "quant": {"direction": "SHORT", "confidence": 0.7},
        "strategy": {"candidate_trade": True, "direction": "SHORT", "confidence": 0.8,
                     "entry": 100.0, "stop": 101.0, "target": 100.5,
                     "reasons": [], "conflicts": []},
        "regime": {"regime": "TRENDING", "confidence": 0.6},
    }
    out = DecisionEngine().decide(agents, "US100", "15m")
    assert out.action == "REJECT"


def test_decision_fail_safe_on_contradiction():
    from app.decision.engine import DecisionEngine

    agents = {
        "technical": {"direction": "SHORT", "confidence": 0.9},
        "quant": {"direction": "SHORT", "confidence": 0.9},
        "strategy": {"candidate_trade": True, "direction": "LONG", "confidence": 0.8,
                     "entry": 100.0, "stop": 99.0, "target": 102.0,
                     "reasons": [], "conflicts": []},
        "regime": {"regime": "RISK_ON", "confidence": 0.6},
    }
    out = DecisionEngine().decide(agents, "US100", "15m")
    assert out.action == "WAIT"
    assert any("contradiction" in c or "conflict" in c for c in out.agent_conflicts)


def test_decision_incomplete_levels_fails_safe():
    from app.decision.engine import DecisionEngine

    agents = {
        "technical": {"direction": "LONG", "confidence": 0.9},
        "strategy": {"candidate_trade": True, "direction": "LONG", "confidence": 0.8,
                     "entry": None, "stop": None, "target": None,
                     "reasons": [], "conflicts": []},
    }
    out = DecisionEngine().decide(agents, "US100", "15m")
    assert out.action == "WAIT"
