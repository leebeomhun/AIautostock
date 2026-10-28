from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db import now_kst, session_scope
from app.market.base import MarketData
from app.market.universe import instrument
from app.models import PaperCash, PaperHolding, PaperOrder


@dataclass
class BrokerHolding:
    symbol: str
    name: str
    qty: int
    avg_price: float
    price: float


@dataclass
class BrokerAccount:
    account_label: str
    cash: float  # 주문 가능 현금
    equity: float  # 추정 예탁자산 (현금 + 평가금액)
    holdings: list[BrokerHolding] = field(default_factory=list)


@dataclass
class OrderResult:
    accepted: bool
    order_no: str = ""
    message: str = ""


@dataclass
class FillStatus:
    order_no: str
    filled_qty: int
    avg_price: float | None
    remaining_qty: int
    fee: float = 0.0
    tax: float = 0.0
    state: str = "submitted"  # submitted | partial | filled | cancelled | rejected


class Broker(Protocol):
    kind: str  # kiwoom | local
    label: str  # 모의투자 | 실전투자 | 로컬 시뮬레이션
    is_live: bool

    def account(self) -> BrokerAccount: ...

    def place_market_order(self, symbol: str, side: str, qty: int) -> OrderResult: ...

    def order_status(self, orders: list[tuple[str, str, int]]) -> dict[str, FillStatus]:
        """orders: (주문번호, 종목코드, 주문수량) 목록 → 주문번호별 체결 현황."""
        ...

    def cancel(self, order_no: str, symbol: str) -> OrderResult: ...


class PaperBroker:
    """키움 키 없이 돌릴 때 쓰는 로컬 모의 브로커. 시장가 주문을 현재가 ± 슬리피지로 즉시 체결합니다."""

    kind = "local"
    label = "로컬 시뮬레이션"
    is_live = False

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        market: MarketData,
        initial_cash: float,
        fee_rate: float,
        sell_tax_rate: float,
        slippage_rate: float,
    ) -> None:
        self.session_factory = session_factory
        self.market = market
        self.fee_rate = fee_rate
        self.sell_tax_rate = sell_tax_rate
        self.slippage_rate = slippage_rate
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        with session_scope(session_factory) as s:
            if s.get(PaperCash, 1) is None:
                s.add(PaperCash(id=1, cash=float(initial_cash)))
            last = s.scalars(select(PaperOrder.order_no).order_by(PaperOrder.created_at.desc())).first()
            if last and last.isdigit():
                self._seq = itertools.count(int(last) + 1)

    def account(self) -> BrokerAccount:
        with session_scope(self.session_factory) as s:
            cash = s.get(PaperCash, 1).cash
            holdings = []
            for h in s.scalars(select(PaperHolding)).all():
                price = self.market.quote(h.symbol).price
                holdings.append(BrokerHolding(h.symbol, h.name, h.qty, h.avg_price, price))
        equity = cash + sum(h.qty * h.price for h in holdings)
        return BrokerAccount("로컬 모의계좌", cash, equity, holdings)

    def place_market_order(self, symbol: str, side: str, qty: int) -> OrderResult:
        if qty <= 0:
            return OrderResult(False, message="수량이 0입니다")
        with self._lock, session_scope(self.session_factory) as s:
            price = self.market.quote(symbol).price
            fill = price * (1 + self.slippage_rate) if side == "buy" else price * (1 - self.slippage_rate)
            fill = round(fill)
            amount = fill * qty
            fee = round(amount * self.fee_rate)
            tax = round(amount * self.sell_tax_rate) if side == "sell" else 0
            cash_row = s.get(PaperCash, 1)
            holding = s.get(PaperHolding, symbol)
            if side == "buy":
                if cash_row.cash < amount + fee:
                    return OrderResult(False, message="주문 가능 현금이 부족합니다")
                cash_row.cash -= amount + fee
                if holding:
                    total = holding.qty + qty
                    holding.avg_price = (holding.avg_price * holding.qty + fill * qty) / total
                    holding.qty = total
                else:
                    s.add(PaperHolding(symbol=symbol, name=instrument(symbol).name, qty=qty, avg_price=fill))
            else:
                if not holding or holding.qty < qty:
                    return OrderResult(False, message="매도 가능 수량이 부족합니다")
                cash_row.cash += amount - fee - tax
                holding.qty -= qty
                if holding.qty == 0:
                    s.delete(holding)
            order_no = f"{next(self._seq):07d}"
            s.add(
                PaperOrder(
                    order_no=order_no, symbol=symbol, side=side, qty=qty, fill_price=fill, fee=fee, tax=tax,
                    created_at=now_kst(),
                )
            )
            return OrderResult(True, order_no, "체결되었습니다")

    def order_status(self, orders: list[tuple[str, str, int]]) -> dict[str, FillStatus]:
        out: dict[str, FillStatus] = {}
        with session_scope(self.session_factory) as s:
            for order_no, _symbol, qty in orders:
                po = s.get(PaperOrder, order_no)
                if po is None:
                    out[order_no] = FillStatus(order_no, 0, None, qty, state="rejected")
                else:
                    out[order_no] = FillStatus(order_no, po.qty, po.fill_price, 0, po.fee, po.tax, "filled")
        return out

    def cancel(self, order_no: str, symbol: str) -> OrderResult:
        return OrderResult(False, message="이미 체결된 주문입니다")
