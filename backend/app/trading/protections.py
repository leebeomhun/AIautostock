"""보호 규칙 — freqtrade 의 Protections(StoplossGuard, CooldownPeriod, MaxDrawdown) 개념을 직접 구현했습니다.

리스크 가드(risk.py)가 '이번 주문이 한도를 넘는가'를 본다면, 보호 규칙은 '최근 결과를 보면 지금 들어가도 되는가'를 봅니다.
- 손절 후 재진입 금지: 손절로 판 종목은 며칠 동안 다시 사지 않습니다.
- 청산 후 대기: 판 종목은 다음 거래일까지 다시 사지 않습니다 (잦은 매매 방지).
- 손절 연속 가드: 하루에 손절이 여러 번 나오면 그날 신규 매수를 멈춥니다.
- 최대 낙폭 가드: 최근 20일 평가자산 고점 대비 낙폭이 기준을 넘으면 신규 매수를 멈춥니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Decision, EquitySnapshot, Order


@dataclass
class ProtectionConfig:
    stoploss_cooldown_days: int = 3
    exit_cooldown_days: int = 1
    stoploss_guard_count: int = 3
    max_drawdown_pct: float = 10.0
    drawdown_lookback_days: int = 20


def _business_days_since(then: datetime, now: datetime) -> int:
    return int(np.busday_count(then.date(), now.date()))


def symbol_block(s: Session, cfg: ProtectionConfig, symbol: str, now: datetime) -> str | None:
    """이 종목을 지금 새로 사도 되는지. 막혀야 하면 이유를 돌려줍니다."""
    since = now - timedelta(days=max(cfg.stoploss_cooldown_days, cfg.exit_cooldown_days) * 2 + 7)
    rows = s.execute(
        select(Order.filled_at, Decision.reason)
        .join(Decision, Decision.id == Order.decision_id, isouter=True)
        .where(Order.symbol == symbol, Order.side == "sell", Order.filled_qty > 0, Order.filled_at >= since)
        .order_by(Order.filled_at.desc())
    ).all()
    for filled_at, reason in rows:
        days = _business_days_since(filled_at, now)
        if reason == "stop_loss" and days < cfg.stoploss_cooldown_days:
            return f"손절 후 재진입 금지 · {cfg.stoploss_cooldown_days}거래일 중 {days}일 지남"
        if days < cfg.exit_cooldown_days:
            return "청산 후 대기 · 다음 거래일부터 다시 살 수 있습니다"
    return None


def account_block(s: Session, cfg: ProtectionConfig, now: datetime) -> str | None:
    """계좌 전체의 신규 매수를 멈춰야 하는지."""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    stops = s.scalar(
        select(func.count())
        .select_from(Order)
        .join(Decision, Decision.id == Order.decision_id)
        .where(Order.side == "sell", Order.filled_qty > 0, Order.filled_at >= start, Decision.reason == "stop_loss")
    ) or 0
    if cfg.stoploss_guard_count and stops >= cfg.stoploss_guard_count:
        return f"오늘 손절 {stops}회 · 손절 연속 가드로 신규 매수를 멈춥니다"
    if cfg.max_drawdown_pct:
        since = now - timedelta(days=cfg.drawdown_lookback_days * 7 // 5 + 1)
        peak = s.scalar(select(func.max(EquitySnapshot.equity)).where(EquitySnapshot.at >= since))
        last = s.scalar(select(EquitySnapshot.equity).order_by(EquitySnapshot.at.desc()).limit(1))
        if peak and last and peak > 0:
            dd = (1 - last / peak) * 100
            if dd >= cfg.max_drawdown_pct:
                return f"최근 {cfg.drawdown_lookback_days}거래일 고점 대비 −{dd:.1f}% · 최대 낙폭 가드(−{cfg.max_drawdown_pct:.0f}%)로 신규 매수를 멈춥니다"
    return None
