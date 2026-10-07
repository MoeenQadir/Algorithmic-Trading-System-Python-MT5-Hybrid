"""Backtest engine (Milestone 17). Event-driven, deterministic, no look-ahead.

Look-ahead prevention contract:
- At bar i the engine shows agents ONLY candles[0..i] (slice, not reference).
- Indicators are recomputed on the visible slice each step (recompute cost is
  acceptable for personal datasets; guarantees no future leakage).
- Fills happen at bar i+1 prices at the earliest (signal on close, fill on
  next bar open), preventing same-bar fill on the signal bar.
- Exits use intrabar high/low of the current bar with conservative ordering
  (stop assumed hit before target when both are touched).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

import numpy as np

from app.analysis import indicators as ta
from app.analysis.strategy import evaluate_setup
from app.config import get_settings
from app.utils.timeutils import utcnow

logger = logging.getLogger("trading.backtest")


def _sizing(equity: float, risk_pct: float, entry: float, stop: float,
            max_risk_pct: float, max_notional: float) -> Optional[dict[str, float]]:
    stop_dist = abs(entry - stop)
    if equity <= 0 or risk_pct <= 0 or stop_dist <= 0 or entry <= 0:
        return None
    risk_amount = min(equity * risk_pct, equity * max_risk_pct)
    qty = risk_amount / stop_dist
    notional = qty * entry
    if notional > max_notional > 0:
        qty *= max_notional / notional
        risk_amount = qty * stop_dist
    return {"qty": qty, "risk_amount": risk_amount}


class BacktestEngine:
    def __init__(self, params: dict[str, Any] | None = None) -> None:
        s = get_settings()
        p = params or {}
        self.strategy_params = {
            "swing_lookback": p.get("swing_lookback", s.swing_lookback),
            "sweep_max_age": p.get("sweep_max_age", s.sweep_max_age),
            "displacement_required": p.get("displacement_required", s.displacement_required),
            "displacement_atr": p.get("displacement_atr", s.displacement_atr),
            "stop_buffer_atr": p.get("stop_buffer_atr", s.stop_buffer_atr),
            "rr_target": p.get("rr_target", 2.0),
            "risk_reward_floor": p.get("risk_reward_floor", s.min_risk_reward),
            "max_atr_pct": p.get("max_atr_pct", 0.02),
        }
        self.risk_pct = float(p.get("risk_pct", s.risk_percentage))
        self.max_risk_pct = float(p.get("max_risk_pct", s.max_risk_per_trade))
        self.max_notional = float(p.get("max_notional", s.max_position_size))
        self.slippage_pct = float(p.get("slippage_pct", s.paper_slippage))
        self.spread_pct = float(p.get("spread_pct", s.paper_spread_pct))
        self.commission_pct = float(p.get("commission_pct", s.paper_commission))
        self.starting_equity = float(p.get("starting_equity", s.paper_account_balance))

    def run(self, candles: list[dict[str, Any]], symbol: str, timeframe: str,
            warmup: int = 80) -> dict[str, Any]:
        """Run the backtest. candles must be ascending by timestamp."""
        if len(candles) <= warmup:
            return {"error": f"need more than {warmup} candles, got {len(candles)}",
                    "run": None, "trades": []}

        equity = self.starting_equity
        cash = equity
        position: Optional[dict[str, Any]] = None
        trades: list[dict[str, Any]] = []
        equity_curve: list[list] = []

        commission_mult = 1.0
        pending_entry: Optional[dict[str, Any]] = None  # signal at bar i -> fill at i+1 open

        for i in range(warmup, len(candles)):
            bar = candles[i]
            visible = candles[: i + 1]  # STRICT: no future candles exposed
            o, h, l, c = (float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"]))

            # ---- 1) fill pending entry at this bar's open -------------------
            if pending_entry is not None and position is None:
                entry_req = pending_entry
                pending_entry = None
                fill_price = o * (1 + self.slippage_pct) if entry_req["direction"] == "LONG" else o * (1 - self.slippage_pct)
                sizing = _sizing(equity, self.risk_pct, fill_price, entry_req["stop"],
                                 self.max_risk_pct, self.max_notional)
                if sizing and sizing["qty"] > 0:
                    fee = fill_price * sizing["qty"] * self.commission_pct
                    cash -= fee
                    position = {
                        "direction": entry_req["direction"], "qty": sizing["qty"],
                        "entry": fill_price, "stop": entry_req["stop"], "target": entry_req["target"],
                        "entry_time": bar["timestamp"], "entry_index": i,
                        "risk_amount": sizing["risk_amount"], "fees_paid": fee,
                        "mae": 0.0, "mfe": 0.0,
                    }

            # ---- 2) manage open position with this bar's H/L (no look-ahead:
            #         we use only the current bar being traded) ----------------
            if position is not None:
                direction = position["direction"]
                move = (l - position["entry"]) if direction == "LONG" else (position["entry"] - h)
                fav = (h - position["entry"]) if direction == "LONG" else (position["entry"] - l)
                risk_per_unit = abs(position["entry"] - position["stop"])
                position["mae"] = min(position["mae"], move / risk_per_unit) if risk_per_unit > 0 else position["mae"]
                position["mfe"] = max(position["mfe"], fav / risk_per_unit) if risk_per_unit > 0 else position["mfe"]

                hit_stop = (l <= position["stop"]) if direction == "LONG" else (h >= position["stop"])
                hit_target = (h >= position["target"]) if direction == "LONG" else (l <= position["target"])

                exit_price: Optional[float] = None
                exit_reason = ""
                if hit_stop:  # conservative: stop before target when both touched
                    exit_price = position["stop"]
                    exit_reason = "stop_loss"
                elif hit_target:
                    exit_price = position["target"]
                    exit_reason = "take_profit"

                if exit_price is not None:
                    slip = exit_price * self.slippage_pct
                    if direction == "LONG":
                        fill = exit_price - slip
                        gross = (fill - position["entry"]) * position["qty"]
                    else:
                        fill = exit_price + slip
                        gross = (position["entry"] - fill) * position["qty"]
                    fee = fill * position["qty"] * self.commission_pct
                    net = gross - fee
                    cash += net
                    equity = cash
                    r_mult = net / position["risk_amount"] if position["risk_amount"] > 0 else 0.0
                    trades.append({
                        "symbol": symbol, "direction": direction,
                        "entry_time": position["entry_time"], "exit_time": bar["timestamp"],
                        "entry_price": round(position["entry"], 10),
                        "exit_price": round(fill, 10), "stop_loss": position["stop"],
                        "take_profit": position["target"], "quantity": position["qty"],
                        "pnl": round(net, 4), "r_multiple": round(r_mult, 4),
                        "exit_reason": exit_reason, "mae": round(position["mae"], 4),
                        "mfe": round(position["mfe"], 4),
                    })
                    position = None

            # ---- 3) evaluate setup on visible slice only ---------------------
            if position is None and pending_entry is None:
                signal = evaluate_setup(visible, self.strategy_params, symbol, timeframe)
                if signal.setup_valid and signal.entry and signal.stop_loss and signal.take_profit:
                    # sanity: target/stop must be on the right side of entry
                    if signal.direction == "LONG" and signal.stop_loss < signal.entry < signal.take_profit:
                        pending_entry = {"direction": "LONG", "stop": signal.stop_loss,
                                         "target": signal.take_profit}
                    elif signal.direction == "SHORT" and signal.take_profit < signal.entry < signal.stop_loss:
                        pending_entry = {"direction": "SHORT", "stop": signal.stop_loss,
                                         "target": signal.take_profit}

            equity = cash + (self._unrealized(position, c) if position else 0.0)
            equity_curve.append([bar["timestamp"].isoformat(), round(equity, 2)])

        # close any open position at the last close
        if position is not None:
            last = candles[-1]
            fill = last["close"] * (1 - self.slippage_pct) if position["direction"] == "LONG" \
                else last["close"] * (1 + self.slippage_pct)
            gross = (fill - position["entry"]) * position["qty"] if position["direction"] == "LONG" \
                else (position["entry"] - fill) * position["qty"]
            fee = fill * position["qty"] * self.commission_pct
            net = gross - fee
            cash += net
            r_mult = net / position["risk_amount"] if position["risk_amount"] > 0 else 0.0
            trades.append({
                "symbol": symbol, "direction": position["direction"],
                "entry_time": position["entry_time"], "exit_time": last["timestamp"],
                "entry_price": round(position["entry"], 10), "exit_price": round(fill, 10),
                "stop_loss": position["stop"], "take_profit": position["target"],
                "quantity": position["qty"], "pnl": round(net, 4), "r_multiple": round(r_mult, 4),
                "exit_reason": "end_of_data", "mae": round(position["mae"], 4),
                "mfe": round(position["mfe"], 4),
            })
            equity = cash
            equity_curve.append([last["timestamp"].isoformat(), round(equity, 2)])

        metrics = self._metrics(trades, equity_curve)
        run = {
            "symbol": symbol, "timeframe": timeframe,
            "start": candles[warmup]["timestamp"].isoformat() if len(candles) > warmup else None,
            "end": candles[-1]["timestamp"].isoformat() if candles else None,
            "params": {
                "strategy": self.strategy_params, "risk_pct": self.risk_pct,
                "slippage_pct": self.slippage_pct, "spread_pct": self.spread_pct,
                "commission_pct": self.commission_pct, "starting_equity": self.starting_equity,
            },
            "starting_equity": self.starting_equity,
            "ending_equity": round(equity, 2),
            "total_pnl": round(equity - self.starting_equity, 2),
            "trade_count": len(trades),
            **metrics,
        }
        return {"run": run, "trades": trades, "equity_curve": equity_curve}

    def _unrealized(self, position: dict[str, Any], price: float) -> float:
        if position["direction"] == "LONG":
            return (price - position["entry"]) * position["qty"]
        return (position["entry"] - price) * position["qty"]

    def _metrics(self, trades: list[dict[str, Any]], equity_curve: list[list]) -> dict[str, Any]:
        n = len(trades)
        if n == 0:
            return {"win_rate": 0.0, "profit_factor": 0.0, "expectancy_r": 0.0,
                    "max_drawdown": 0.0, "sharpe": None, "sortino": None,
                    "consecutive_wins": 0, "consecutive_losses": 0,
                    "avg_r": 0.0, "monthly": {}, "yearly": {}}
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] < 0]
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        rs = [t["r_multiple"] or 0.0 for t in trades]
        expectancy_r = sum(rs) / n

        curve_values = [e[1] for e in equity_curve]
        peak = curve_values[0]
        max_dd = 0.0
        for v in curve_values:
            peak = max(peak, v)
            if peak > 0:
                max_dd = max(max_dd, (peak - v) / peak)

        # bar-level returns for Sharpe/Sortino
        returns = []
        for a, b in zip(curve_values, curve_values[1:]):
            returns.append((b - a) / a if a else 0.0)
        sharpe = sortino = None
        if len(returns) > 1:
            mean_r, std_r = float(np.mean(returns)), float(np.std(returns))
            if std_r > 0:
                sharpe = round(mean_r / std_r * (len(returns) ** 0.5), 3)
            downside = [r for r in returns if r < 0]
            if downside:
                dstd = float(np.std(downside))
                if dstd > 0:
                    sortino = round(mean_r / dstd * (len(returns) ** 0.5), 3)

        def streak(seq: list[bool]) -> int:
            best = cur = 0
            for flag in seq:
                cur = cur + 1 if flag else 0
                best = max(best, cur)
            return best

        monthly: dict[str, float] = {}
        yearly: dict[str, float] = {}
        for t in trades:
            ts = t.get("exit_time")
            if hasattr(ts, "strftime"):
                monthly[ts.strftime("%Y-%m")] = round(monthly.get(ts.strftime("%Y-%m"), 0.0) + t["pnl"], 2)
                yearly[ts.strftime("%Y")] = round(yearly.get(ts.strftime("%Y"), 0.0) + t["pnl"], 2)

        return {
            "win_rate": round(len(wins) / n, 4),
            "loss_rate": round(len(losses) / n, 4),
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
            "expectancy_r": round(expectancy_r, 4),
            "avg_r": round(expectancy_r, 4),
            "max_drawdown": round(max_dd, 4),
            "sharpe": sharpe,
            "sortino": sortino,
            "consecutive_wins": streak([t["pnl"] > 0 for t in trades]),
            "consecutive_losses": streak([t["pnl"] < 0 for t in trades]),
            "monthly": monthly,
            "yearly": yearly,
        }

