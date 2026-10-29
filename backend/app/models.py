from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, now_kst


class AccountSettings(Base):
    """계좌 단위 설정 (단일 행, id=1)."""

    __tablename__ = "account_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    onboarded: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_profile: Mapped[str] = mapped_column(String(20), default="neutral")
    invest_amount: Mapped[int] = mapped_column(Integer, default=20_000_000)
    daily_loss_limit: Mapped[int] = mapped_column(Integer, default=300_000)
    limit_action: Mapped[str] = mapped_column(String(10), default="buy")  # buy | all
    agreed: Mapped[bool] = mapped_column(Boolean, default=False)
    mode: Mapped[str] = mapped_column(String(10), default="approve")  # notify | approve | auto
    running: Mapped[bool] = mapped_column(Boolean, default=True)
    paper_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    live_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kst)


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    template: Mapped[str] = mapped_column(String(20))  # momentum | meanrev | news | value
    version: Mapped[str] = mapped_column(String(20), default="v1.0")
    universe: Mapped[str] = mapped_column(String(20), default="k200")
    custom_symbols: Mapped[list[str]] = mapped_column(JSON, default=list)
    exclude_risky: Mapped[bool] = mapped_column(Boolean, default=True)
    allocation: Mapped[int] = mapped_column(Integer)
    stop_pct: Mapped[float] = mapped_column(Float, default=5)
    take_pct: Mapped[float] = mapped_column(Float, default=10)
    max_weight_pct: Mapped[float] = mapped_column(Float, default=15)
    daily_loss_limit: Mapped[int] = mapped_column(Integer, default=300_000)
    max_orders_per_day: Mapped[int] = mapped_column(Integer, default=20)
    exec_mode: Mapped[str] = mapped_column(String(10), default="approve")  # 새 전략은 api.routes.default_exec_mode 로 정합니다 (모의 auto · 실거래 approve)
    status: Mapped[str] = mapped_column(String(10), default="running")  # running | paused | stopped
    pause_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    paused_auto: Mapped[bool] = mapped_column(Boolean, default=False)
    last_rebalanced: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kst)


class Position(Base):
    """AI가 연 포지션의 전략별 장부. 사용자가 직접 산 종목은 여기에 없고, AI는 이 장부에 있는 수량만 매도합니다."""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"))
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    name: Mapped[str] = mapped_column(String(60))
    qty: Mapped[int] = mapped_column(Integer)
    avg_price: Mapped[float] = mapped_column(Float)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=now_kst)
    partial_taken: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_note: Mapped[str] = mapped_column(String(60), default="신규 진입")
    ai_note_tone: Mapped[str] = mapped_column(String(10), default="info")


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"))
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    name: Mapped[str] = mapped_column(String(60))
    side: Mapped[str] = mapped_column(String(4))  # buy | sell
    reason: Mapped[str] = mapped_column(String(30))
    qty: Mapped[int] = mapped_column(Integer)
    max_qty: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    avg_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    order_type: Mapped[str] = mapped_column(String(10), default="market")
    confidence: Mapped[float] = mapped_column(Float)
    summary: Mapped[str] = mapped_column(Text)
    caution: Mapped[str] = mapped_column(Text, default="")
    signals: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    history_stat: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    risk_rules: Mapped[list[str]] = mapped_column(JSON, default=list)
    risk_notes: Mapped[list[str]] = mapped_column(JSON, default=list)
    chart: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    model: Mapped[str] = mapped_column(String(60))
    strategy_label: Mapped[str] = mapped_column(String(80))
    data_at: Mapped[datetime] = mapped_column(DateTime)
    mode: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(12), index=True)
    # pending | notified | executed | rejected | expired | cancelled | blocked | failed
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kst, index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(10), nullable=True)  # user | auto | system
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    decision_id: Mapped[str | None] = mapped_column(ForeignKey("decisions.id"), nullable=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"))
    broker_order_no: Mapped[str] = mapped_column(String(20), index=True)
    symbol: Mapped[str] = mapped_column(String(12))
    name: Mapped[str] = mapped_column(String(60))
    side: Mapped[str] = mapped_column(String(4))
    qty: Mapped[int] = mapped_column(Integer)
    filled_qty: Mapped[int] = mapped_column(Integer, default=0)
    applied_qty: Mapped[int] = mapped_column(Integer, default=0)  # 장부(Position)에 반영한 체결 수량
    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="submitted")
    # submitted | partial | filled | cancelled | rejected
    fee: Mapped[float] = mapped_column(Float, default=0)
    tax: Mapped[float] = mapped_column(Float, default=0)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    hold_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kst, index=True)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Activity(Base):
    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=now_kst, index=True)
    kind: Mapped[str] = mapped_column(String(20))
    # buy_fill | sell_fill | approval | order_sent | risk_guard | strategy | system | notify | ai_hold
    title: Mapped[str] = mapped_column(String(120))
    detail: Mapped[str] = mapped_column(Text, default="")
    decision_id: Mapped[str | None] = mapped_column(ForeignKey("decisions.id"), nullable=True)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, index=True)
    equity: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    benchmark: Mapped[float] = mapped_column(Float)


class DayState(Base):
    __tablename__ = "day_states"

    date: Mapped[str] = mapped_column(String(10), primary_key=True)
    start_equity: Mapped[float] = mapped_column(Float)
    alert_level: Mapped[int] = mapped_column(Integer, default=0)  # 0 | 50 | 80 | 100
    halted: Mapped[bool] = mapped_column(Boolean, default=False)


class PaperCash(Base):
    """로컬 시뮬레이션 브로커의 현금 (키움을 쓰지 않을 때만 사용)."""

    __tablename__ = "paper_cash"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    cash: Mapped[float] = mapped_column(Float)


class PaperHolding(Base):
    __tablename__ = "paper_holdings"

    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    qty: Mapped[int] = mapped_column(Integer)
    avg_price: Mapped[float] = mapped_column(Float)


class PaperOrder(Base):
    __tablename__ = "paper_orders"

    order_no: Mapped[str] = mapped_column(String(20), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12))
    side: Mapped[str] = mapped_column(String(4))
    qty: Mapped[int] = mapped_column(Integer)
    fill_price: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float)
    tax: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kst)


class LedgerEntry(Base):
    """해시 체인 판단 원장 (app.ledger)."""

    __tablename__ = "ledger"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    at: Mapped[datetime] = mapped_column(DateTime)
    kind: Mapped[str] = mapped_column(String(40))
    ref: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)
