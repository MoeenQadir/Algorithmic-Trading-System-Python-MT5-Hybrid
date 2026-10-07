"""Execution layer (Milestone 16) + live-broker architecture (Section 10).

BrokerInterface is the seam for future live brokers. The only active
implementation is PaperBroker (PAPER_TRADING=true by default). RobinhoodBroker
is an explicitly disabled adapter placeholder kept isolated.
"""
from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.database import Fill, IdempotencyKey, MarketData, Order, Position, Trade
from app.utils.timeutils import utcnow

logger = logging.getLogger("trading.broker")


def _finite(value: Any) -> bool:
    """True for finite real numbers (rejects NaN, Infinity, non-numeric)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value == value and value not in (float("inf"), float("-inf"))


def validate_stop_targets(side: str, stop_price: float | None, take_profit: float | None,
                          entry_hint: float | None = None, require_stops: bool = False) -> tuple[bool, str]:
    """Phase 39: validate SL/TP geometry BEFORE an order is accepted.

    BUY:  SL < ENTRY < TP
    SELL: TP < ENTRY < SL

    With require_stops=True (strategy/risk path and manual API orders) missing
    SL/TP is rejected. Manual engine-internal orders may omit them for
    exit-engine testing; provided values are always geometry-checked against
    the entry. Rejects NaN/Infinity/zero/negative. Returns (ok, error).
    """
    side = side.upper()
    for name, value in (("stop_loss", stop_price), ("take_profit", take_profit)):
        if value is None:
            if require_stops:
                return False, f"missing {name}"
            continue
        if not _finite(value) or value <= 0:
            return False, f"invalid {name}: {value!r}"
    if entry_hint is not None:
        if not _finite(entry_hint) or entry_hint <= 0:
            return False, f"invalid entry: {entry_hint!r}"
        if stop_price is not None and take_profit is not None:
            if side == "BUY" and not (stop_price < entry_hint < take_profit):
                return False, f"BUY requires SL < ENTRY < TP (got SL={stop_price}, entry={entry_hint}, TP={take_profit})"
            if side == "SELL" and not (take_profit < entry_hint < stop_price):
                return False, f"SELL requires TP < ENTRY < SL (got TP={take_profit}, entry={entry_hint}, SL={stop_price})"
    return True, ""


class BrokerInterface(ABC):
    """Contract every broker must satisfy."""

    @abstractmethod
    def get_account(self) -> dict[str, Any]: ...
    @abstractmethod
    def get_positions(self) -> list[dict[str, Any]]: ...
    @abstractmethod
    def get_quote(self, symbol: str) -> dict[str, Any] | None: ...
    @abstractmethod
    def place_order(self, symbol: str, side: str, quantity: float, order_type: str = "market",
                    limit_price: float | None = None, stop_price: float | None = None,
                    take_profit: float | None = None, idempotency_key: str | None = None) -> dict[str, Any]: ...
    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> bool: ...
    @abstractmethod
    def modify_order(self, broker_order_id: str, quantity: float | None = None,
                     limit_price: float | None = None, stop_price: float | None = None) -> bool: ...
    @abstractmethod
    def get_order(self, broker_order_id: str) -> dict[str, Any] | None: ...
    @abstractmethod
    def get_orders(self, status: str | None = None) -> list[dict[str, Any]]: ...


class PaperBroker(BrokerInterface):
    """Deterministic simulated broker: spreads, slippage, commission, fees.

    Market orders fill immediately at bid/ask with slippage. Limit/stop orders
    rest until `on_candle` (live polling) or the backtest engine feeds prices.
    """

    def __init__(self, db: Session, *, starting_balance: float | None = None) -> None:
        self.db = db
        s = get_settings()
        self.starting_balance = starting_balance if starting_balance is not None else s.paper_account_balance
        self.commission_rate = s.paper_commission
        self.slippage_pct = s.paper_slippage
        self.spread_pct = s.paper_spread_pct

    # ------------------------------------------------------------------ account
    def get_account(self) -> dict[str, Any]:
        s = get_settings()
        positions = self.db.query(Position).filter(Position.status == "open").all()
        cash = self._cash()
        last_prices = {p.symbol: self._last_price(p.symbol) for p in positions}
        unrealized = 0.0
        for p in positions:
            px = last_prices.get(p.symbol)
            if px is None:
                px = p.entry_price
            mult = self._multiplier()
            if p.direction == "LONG":
                unrealized += (px - p.entry_price) * p.quantity * mult
            else:
                unrealized += (p.entry_price - px) * p.quantity * mult
        realized = self._realized_pnl()
        equity = cash + unrealized
        return {
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "unrealized_pnl": round(unrealized, 2),
            "realized_pnl": round(realized, 2),
            "open_positions": len(positions),
            "starting_balance": self.starting_balance,
            "currency": "USD",
            "paper": True,
        }

    # ------------------------------------------------------------------ helpers
    def _multiplier(self) -> float:
        return 1.0

    def _cash(self) -> float:
        """Cash = starting balance +/- realized P&L - commissions and fees paid."""
        realized = self._realized_pnl()
        fees = sum(f.commission or 0.0 for f in self.db.query(Fill).all())
        return self.starting_balance + realized - fees

    def _realized_pnl(self) -> float:
        total = 0.0
        closed = self.db.query(Trade).filter(Trade.status == "closed").all()
        for t in closed:
            total += t.pnl or 0.0
        return total

    def _last_price(self, symbol: str) -> float | None:
        candle = (
            self.db.query(MarketData)
            .filter(MarketData.symbol == symbol)
            .order_by(MarketData.timestamp.desc())
            .first()
        )
        return float(candle.close) if candle else None

    def _quote(self, symbol: str, ref_price: float) -> tuple[float, float]:
        """Deterministic synthetic bid/ask around ref price from config spread."""
        half = ref_price * self.spread_pct / 2.0
        return ref_price - half, ref_price + half

    def _apply_slippage(self, price: float, side: str) -> float:
        slip = price * self.slippage_pct
        return price + slip if side == "BUY" else price - slip

    # ------------------------------------------------------------------ orders
    def place_order(self, symbol: str, side: str, quantity: float, order_type: str = "market",
                    limit_price: float | None = None, stop_price: float | None = None,
                    take_profit: float | None = None, idempotency_key: str | None = None,
                    require_stops: bool = False) -> dict[str, Any]:
        side = side.upper()
        if side not in {"BUY", "SELL"}:
            return {"ok": False, "error": f"invalid side '{side}'"}
        if not _finite(quantity) or quantity <= 0:
            return {"ok": False, "error": f"invalid quantity: {quantity!r}"}
        if order_type not in {"market", "limit", "stop"}:
            return {"ok": False, "error": f"invalid order type '{order_type}'"}
        # Phase 39: reject missing/invalid SL/TP and impossible geometry.
        ok, err = validate_stop_targets(side, stop_price, take_profit, require_stops=require_stops)
        if not ok:
            return {"ok": False, "error": err}

        # Check for duplicate order using idempotency key
        if idempotency_key:
            import hashlib
            payload_hash = hashlib.sha256(f"{symbol.upper()}{side}{quantity}{order_type}".encode()).hexdigest()
            existing_key = (
                self.db.query(IdempotencyKey)
                .filter(IdempotencyKey.key == idempotency_key, IdempotencyKey.operation == "place_order")
                .first()
            )
            if existing_key:
                return {"ok": True, "duplicate": True, "message": f"Duplicate order detected (key={idempotency_key})"}

        order = Order(
            broker_order_id=uuid.uuid4().hex[:16],
            symbol=symbol.upper(), side=side, order_type=order_type, quantity=float(quantity),
            limit_price=limit_price, stop_price=stop_price, take_profit=take_profit,
            status="pending" if order_type in {"limit", "stop"} else "filled",
        )
        self.db.add(order)
        self.db.flush()

        # Record idempotency key
        if idempotency_key:
            import hashlib
            payload_hash = hashlib.sha256(f"{symbol.upper()}{side}{quantity}{order_type}".encode()).hexdigest()
            self.db.add(IdempotencyKey(
                key=idempotency_key,
                operation="place_order",
                symbol=symbol.upper(),
                payload_hash=payload_hash,
                processed_at=utcnow(),
            ))

        if order_type == "market":
            result = self._fill_market(order)
            self.db.commit()
            return {"ok": result[0], "error": result[1], "order": _order_dict(order)}
        self.db.commit()
        return {"ok": True, "order": _order_dict(order)}

    def _fill_market(self, order: Order) -> tuple[bool, str]:
        ref = self._last_price(order.symbol)
        if ref is None:
            order.status = "rejected"
            return False, f"no market data for {order.symbol}"
        # Phase 39: entry now known -> enforce SL/TP geometry against the fill.
        ok, err = validate_stop_targets(order.side, order.stop_price, order.take_profit, entry_hint=ref)
        if not ok:
            order.status = "rejected"
            return False, err
        bid, ask = self._quote(order.symbol, ref)
        raw = ask if order.side == "BUY" else bid
        fill_price = self._apply_slippage(raw, order.side)
        commission = fill_price * order.quantity * self.commission_rate
        fill = Fill(order_id=order.id, quantity=order.quantity, price=round(fill_price, 10),
                    commission=round(commission, 10), slippage=0.0)
        self.db.add(fill)
        order.status = "filled"
        order.filled_quantity = order.quantity
        order.avg_fill_price = round(fill_price, 10)
        self._open_or_extend_position(order, fill)
        return True, ""

    def _open_or_extend_position(self, order: Order, fill: Fill) -> None:
        direction = "LONG" if order.side == "BUY" else "SHORT"
        existing = (
            self.db.query(Position)
            .filter(Position.symbol == order.symbol, Position.status == "open", Position.direction == direction)
            .first()
        )
        if existing:
            total_qty = existing.quantity + fill.quantity
            existing.entry_price = ((existing.entry_price * existing.quantity) + (fill.price * fill.quantity)) / total_qty
            existing.quantity = total_qty
            position = existing
        else:
            position = Position(
                symbol=order.symbol, direction=direction, quantity=fill.quantity,
                entry_price=fill.price, stop_loss=order.stop_price,
                take_profit=order.take_profit,
                status="open",
            )
            self.db.add(position)
            self.db.flush()
        # link a Trade row (journal entry, M19)
        trade = Trade(
            symbol=order.symbol, direction=direction, entry_time=utcnow(),
            entry_price=fill.price, stop_loss=order.stop_price, take_profit=order.take_profit,
            quantity=fill.quantity, status="open",
            order_id=order.id, position_id=position.id,
            execution_info=f'{{"commission": {fill.commission}, "fill_price": {fill.price}}}',
        )
        self.db.add(trade)
        order.related_position_id = position.id

    def close_position(self, position_id: int, reason: str = "manual", exit_price: float | None = None) -> dict[str, Any]:
        position = self.db.get(Position, position_id)
        if not position or position.status != "open":
            return {"ok": False, "error": "position not open"}
        ref = exit_price if exit_price is not None else self._last_price(position.symbol)
        if ref is None:
            return {"ok": False, "error": f"no market data for {position.symbol}"}
        side = "SELL" if position.direction == "LONG" else "BUY"
        bid, ask = self._quote(position.symbol, ref)
        raw = bid if side == "SELL" else ask
        fill_price = self._apply_slippage(raw, side)
        commission = fill_price * position.quantity * self.commission_rate
        pnl = (fill_price - position.entry_price) * position.quantity if position.direction == "LONG" \
            else (position.entry_price - fill_price) * position.quantity
        pnl -= commission
        r_multiple = None
        if position.stop_loss is not None:
            risk_per_unit = abs(position.entry_price - position.stop_loss)
            if risk_per_unit > 0:
                pnl_per_unit = (fill_price - position.entry_price) if position.direction == "LONG" else (position.entry_price - fill_price)
                r_multiple = round(pnl_per_unit / risk_per_unit, 4)
        position.status = "closed"
        position.closed_at = utcnow()
        position.close_price = round(fill_price, 10)
        trade = (
            self.db.query(Trade)
            .filter(Trade.position_id == position.id, Trade.status == "open")
            .first()
        )
        if trade:
            trade.exit_time = utcnow()
            trade.exit_price = round(fill_price, 10)
            trade.pnl = round(pnl, 4)
            trade.r_multiple = r_multiple
            trade.status = "closed"
            trade.result = "win" if pnl > 0 else ("loss" if pnl < 0 else "breakeven")
            trade.reason = reason
        order = Order(broker_order_id=uuid.uuid4().hex[:16], symbol=position.symbol, side=side,
                      order_type="market", quantity=position.quantity, status="filled",
                      filled_quantity=position.quantity, avg_fill_price=round(fill_price, 10))
        self.db.add(order)
        self.db.flush()
        self.db.add(Fill(order_id=order.id, quantity=position.quantity, price=round(fill_price, 10),
                         commission=round(commission, 10), slippage=0.0))
        self.db.commit()
        return {"ok": True, "pnl": round(pnl, 4), "r_multiple": r_multiple, "fill_price": round(fill_price, 10)}

    def check_stops_and_targets(self) -> list[dict[str, Any]]:
        """Trigger SL/TP for open positions against the latest price (poll mode)."""
        s = get_settings()
        results = []
        for position in self.db.query(Position).filter(Position.status == "open").all():
            ref = self._last_price(position.symbol)
            if ref is None:
                continue
            hit_sl = position.stop_loss is not None and (
                (ref <= position.stop_loss and position.direction == "LONG")
                or (ref >= position.stop_loss and position.direction == "SHORT")
            )
            hit_tp = position.take_profit is not None and (
                (ref >= position.take_profit and position.direction == "LONG")
                or (ref <= position.take_profit and position.direction == "SHORT")
            )
            if hit_sl or hit_tp:
                reason = "stop_loss" if hit_sl else "take_profit"
                results.append({"position_id": position.id, "symbol": position.symbol,
                                "reason": reason, **self.close_position(position.id, reason)})
        return results

    # ------------------------------------------------------- resting orders
    def process_pending_orders(self) -> list[dict[str, Any]]:
        """Fill limit/stop orders whose trigger conditions are met."""
        results = []
        for order in self.db.query(Order).filter(Order.status == "pending").all():
            ref = self._last_price(order.symbol)
            if ref is None:
                continue
            should_fill = False
            if order.order_type == "limit":
                should_fill = (order.limit_price is not None and ref <= order.limit_price and order.side == "BUY") or \
                              (order.limit_price is not None and ref >= order.limit_price and order.side == "SELL")
            elif order.order_type == "stop":
                should_fill = (order.stop_price is not None and ref >= order.stop_price and order.side == "BUY") or \
                              (order.stop_price is not None and ref <= order.stop_price and order.side == "SELL")
            if should_fill:
                fill_price = order.limit_price if order.order_type == "limit" else order.stop_price
                commission = fill_price * order.quantity * self.commission_rate
                fill = Fill(order_id=order.id, quantity=order.quantity, price=round(fill_price, 10),
                            commission=round(commission, 10), slippage=0.0)
                self.db.add(fill)
                order.status = "filled"
                order.filled_quantity = order.quantity
                order.avg_fill_price = round(fill_price, 10)
                self._open_or_extend_position(order, fill)
                results.append({"order_id": order.id, "filled": True, "price": round(fill_price, 10)})
        self.db.commit()
        return results

    # ---------------------------------------------------------------- Broker API
    def get_positions(self) -> list[dict[str, Any]]:
        out = []
        for p in self.db.query(Position).filter(Position.status == "open").all():
            last = self._last_price(p.symbol) or p.entry_price
            mult = self._multiplier()
            unreal = (last - p.entry_price) * p.quantity * mult if p.direction == "LONG" else \
                     (p.entry_price - last) * p.quantity * mult
            out.append({
                "id": p.id, "symbol": p.symbol, "direction": p.direction, "quantity": p.quantity,
                "entry_price": p.entry_price, "stop_loss": p.stop_loss, "take_profit": p.take_profit,
                "opened_at": p.opened_at.isoformat(), "current_price": last,
                "unrealized_pnl": round(unreal, 4),
            })
        return out

    def get_quote(self, symbol: str) -> dict[str, Any] | None:
        ref = self._last_price(symbol)
        if ref is None:
            return None
        bid, ask = self._quote(symbol, ref)
        return {"symbol": symbol.upper(), "bid": bid, "ask": ask, "last": ref,
                "spread": round(ask - bid, 10), "source": "paper"}

    def cancel_order(self, broker_order_id: str) -> bool:
        order = self.db.query(Order).filter(Order.broker_order_id == broker_order_id).first()
        if not order or order.status != "pending":
            return False
        order.status = "cancelled"
        self.db.commit()
        return True

    def modify_order(self, broker_order_id: str, quantity: float | None = None,
                     limit_price: float | None = None, stop_price: float | None = None) -> bool:
        order = self.db.query(Order).filter(Order.broker_order_id == broker_order_id).first()
        if not order or order.status != "pending":
            return False
        if quantity is not None and quantity > 0:
            order.quantity = quantity
        if limit_price is not None and limit_price > 0:
            order.limit_price = limit_price
        if stop_price is not None and stop_price > 0:
            order.stop_price = stop_price
        self.db.commit()
        return True

    def get_order(self, broker_order_id: str) -> dict[str, Any] | None:
        order = self.db.query(Order).filter(Order.broker_order_id == broker_order_id).first()
        return _order_dict(order) if order else None

    def get_orders(self, status: str | None = None) -> list[dict[str, Any]]:
        q = self.db.query(Order).order_by(Order.created_at.desc())
        if status:
            q = q.filter(Order.status == status)
        return [_order_dict(o) for o in q.limit(200).all()]


def _order_dict(order: Order) -> dict[str, Any]:
    return {
        "id": order.id, "broker_order_id": order.broker_order_id, "symbol": order.symbol,
        "side": order.side, "order_type": order.order_type, "quantity": order.quantity,
        "limit_price": order.limit_price, "stop_price": order.stop_price,
        "take_profit": order.take_profit, "status": order.status,
        "filled_quantity": order.filled_quantity, "avg_fill_price": order.avg_fill_price,
        "created_at": order.created_at.isoformat(),
    }


class RobinhoodBroker(BrokerInterface):
    """DISABLED live adapter (Milestone 10 / Section 10 of spec).

    Kept as an isolated seam to prove the architecture supports live brokers.
    Raises unless LIVE_TRADING=true AND credentials are configured; the app
    never instantiates this by default.
    """

    name = "robinhood"

    def __init__(self) -> None:
        s = get_settings()
        if not s.live_trading:
            raise RuntimeError("LIVE_TRADING is disabled; RobinhoodBroker is not available")

    def _unimplemented(self) -> None:
        raise NotImplementedError("Robinhood adapter is intentionally disabled in this build")

    def get_account(self): self._unimplemented()
    def get_positions(self): self._unimplemented()
    def get_quote(self, symbol): self._unimplemented()
    def place_order(self, *args, **kwargs): self._unimplemented()
    def cancel_order(self, broker_order_id): self._unimplemented()
    def modify_order(self, *args, **kwargs): self._unimplemented()
    def get_order(self, broker_order_id): self._unimplemented()
    def get_orders(self, status=None): self._unimplemented()


def get_broker(db: Session) -> PaperBroker:
    """Factory: always PaperBroker unless live trading is explicitly enabled."""
    s = get_settings()
    if s.live_trading:
        logger.error("LIVE_TRADING enabled but no live broker implemented; falling back to paper")
    return PaperBroker(db)
