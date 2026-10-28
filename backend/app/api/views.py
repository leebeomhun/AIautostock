"""화면별 응답 조립. 계산은 서버에서 하고, 화면은 표시만 합니다."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.container import Container
from app.db import iso_kst, now_kst, session_scope
from app.market.universe import instrument, universe_label
from app.models import AccountSettings, Activity, Decision, EquitySnapshot, Order, Position, Strategy
from app.quant import timeseries as ts
from app.quant.signals import TEMPLATES
from app.trading.broker import BrokerAccount
from app.trading.engine import MODE_LABEL

WEEKDAYS = "월화수목금토일"
STATUS_LABEL = {"running": "실행 중", "paused": "일시정지", "stopped": "중지"}
DECISION_STATUS = {
    "pending": "승인 대기",
    "notified": "알림 보냄",
    "executed": "주문 전송됨",
    "rejected": "거절함",
    "expired": "자동 취소",
    "cancelled": "취소됨",
    "held": "AI 보류",
    "failed": "주문 실패",
}


def _start_of_day(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def meta(c: Container) -> dict[str, Any]:
    return {
        "app_name": c.settings.app_name,
        "broker": {"kind": c.broker.kind, "label": c.broker.label, "is_live": c.broker.is_live},
        "market_source": c.market.name,
        "ai": {"enabled": c.analyst.enabled, "model": c.analyst.label},
    }


def _account(c: Container) -> tuple[BrokerAccount | None, str | None]:
    try:
        return c.engine.account(), None
    except Exception as exc:  # noqa: BLE001
        return None, f"계좌 조회 실패: {exc}"


def _realized_today(s: Session, now: datetime) -> tuple[float, int]:
    start = _start_of_day(now)
    realized = s.scalar(select(func.coalesce(func.sum(Order.realized_pnl), 0)).where(Order.filled_at >= start)) or 0
    fills = s.scalar(select(func.count()).select_from(Order).where(Order.filled_at >= start)) or 0
    return float(realized), int(fills)


def _win_stats(s: Session, now: datetime) -> dict[str, Any]:
    since = now - timedelta(days=30)
    rows = s.scalars(
        select(Order).where(Order.side == "sell", Order.filled_at >= since, Order.realized_pnl.is_not(None))
    ).all()
    wins = sum(1 for o in rows if (o.realized_pnl or 0) > 0)
    losses = len(rows) - wins
    holds = [o.hold_days for o in rows if o.hold_days is not None]
    return {
        "win_rate": round(wins / len(rows) * 100, 1) if rows else None,
        "wins": wins,
        "losses": losses,
        "avg_hold_days": round(sum(holds) / len(holds), 1) if holds else None,
    }


def _strategy_rows(c: Container, s: Session, equity: float) -> list[dict[str, Any]]:
    out = []
    for st in s.scalars(select(Strategy).where(Strategy.status != "stopped").order_by(Strategy.id)).all():
        positions = s.scalars(select(Position).where(Position.strategy_id == st.id)).all()
        day_pnl = invested = 0.0
        for p in positions:
            try:
                q = c.market.quote(p.symbol)
            except Exception:  # noqa: BLE001
                continue
            day_pnl += p.qty * (q.price - q.prev_close)
            invested += p.qty * q.price
        note = st.pause_reason if st.status == "paused" else None
        if st.template == "news" and not c.analyst.enabled:
            note = "OpenAI 키가 없어 뉴스 분석을 하지 않습니다 · 신규 진입 없음"
        out.append(
            {
                "id": st.id,
                "name": st.name,
                "template": st.template,
                "status": st.status,
                "status_label": STATUS_LABEL[st.status],
                "universe_label": universe_label(st.universe),
                "alloc_pct": round(st.allocation / equity * 100) if equity else 0,
                "invested_pct": round(invested / equity * 100, 1) if equity else 0,
                "today_pct": round(day_pnl / st.allocation * 100, 2) if st.allocation else 0,
                "positions": len(positions),
                "note": note,
                "exec_mode": st.exec_mode,
            }
        )
    return out


def _holdings(c: Container, s: Session, acct: BrokerAccount) -> list[dict[str, Any]]:
    ledger: dict[str, list[Position]] = {}
    for p in s.scalars(select(Position)).all():
        ledger.setdefault(p.symbol, []).append(p)
    strategies = {st.id: st for st in s.scalars(select(Strategy)).all()}
    pending = {d.symbol: d.id for d in s.scalars(select(Decision).where(Decision.status == "pending")).all()}
    rows = []
    for h in acct.holdings:
        pos = ledger.get(h.symbol, [])
        strat_names = sorted({strategies[p.strategy_id].name for p in pos if p.strategy_id in strategies})
        note, tone = ("AI 관리 안 함", "muted")
        if pos:
            p0 = pos[0]
            note, tone = p0.ai_note, p0.ai_note_tone
            st = strategies.get(p0.strategy_id)
            if st and st.status == "paused" and tone != "warn":
                note, tone = "관망 · 전략 정지 중", "info"
        pnl = (h.price - h.avg_price) * h.qty
        rows.append(
            {
                "symbol": h.symbol,
                "name": h.name or instrument(h.symbol).name,
                "qty": h.qty,
                "avg_price": round(h.avg_price),
                "price": round(h.price),
                "pnl": round(pnl),
                "pnl_pct": round((h.price / h.avg_price - 1) * 100, 2) if h.avg_price else 0,
                "strategy": ", ".join(strat_names) if strat_names else "직접 보유",
                "ai_note": note,
                "ai_tone": tone,
                "decision_id": pending.get(h.symbol),
            }
        )
    rows.sort(key=lambda r: r["qty"] * r["price"], reverse=True)
    return rows


FEED_TAGS = {
    "buy_fill": ("매수 체결", "buy"),
    "sell_fill": ("매도 체결", "sell"),
    "order_sent": ("주문 전송", "info"),
    "risk_guard": ("리스크 가드", "info"),
    "strategy": ("전략", "warn"),
    "ai_hold": ("AI 보류", "info"),
    "system": ("시스템", "info"),
}


def _feed(s: Session, now: datetime, limit: int = 12) -> list[dict[str, Any]]:
    acts = s.scalars(select(Activity).order_by(Activity.at.desc(), Activity.id.desc()).limit(limit * 2)).all()
    decision_ids = {a.decision_id for a in acts if a.decision_id}
    decisions = {d.id: d for d in s.scalars(select(Decision).where(Decision.id.in_(decision_ids))).all()} if decision_ids else {}
    approval_ids = {a.decision_id for a in acts if a.kind == "approval"}
    out = []
    for a in acts:
        if a.kind == "order_sent" and a.decision_id in approval_ids:
            continue  # 승인 카드에서 이미 결과를 보여 줍니다
        d = decisions.get(a.decision_id) if a.decision_id else None
        tag, tone = FEED_TAGS.get(a.kind, ("기록", "info"))
        pending = False
        decided_text = None
        detail = a.detail
        if a.kind == "strategy" and "정지" in a.title:
            tag = "전략 정지"
        if a.kind == "notify" and d:
            tag, tone = ("매수 신호", "buy") if d.side == "buy" else ("매도 신호", "sell")
        if a.kind == "approval" and d:
            side_ko = "매수" if d.side == "buy" else "매도"
            if d.status == "pending":
                tag, tone, pending = "승인 필요", "warn", True
                if d.expires_at:
                    left = max(0, int((d.expires_at - now).total_seconds()))
                    detail = f"{a.detail} · {left // 60}분 {left % 60}초 후 자동 취소"
            elif d.status == "executed":
                tag, tone = f"{side_ko} 주문", d.side
                decided_text = f"승인됨 · 시장가 {side_ko} 주문을 보냈습니다. 체결되면 알려드릴게요."
            elif d.status == "rejected":
                tag, tone = "거절함", "info"
                keep = "계속 보유합니다" if d.side == "sell" else "매수하지 않습니다"
                decided_text = f"거절됨 · {keep}. 이 결정은 AI 피드백으로 기록됩니다."
            else:
                tag, tone = DECISION_STATUS.get(d.status, "기록"), "info"
                decided_text = d.status_note or DECISION_STATUS.get(d.status)
        out.append(
            {
                "id": a.id,
                "time": a.at.strftime("%H:%M") if a.at.date() == now.date() else a.at.strftime("%m/%d"),
                "tag": tag,
                "tone": tone,
                "title": a.title,
                "detail": detail,
                "decision_id": a.decision_id,
                "pending": pending,
                "decided_text": decided_text,
                "expires_at": iso_kst(d.expires_at) if pending and d else None,
            }
        )
        if len(out) >= limit:
            break
    return out


def _pending(s: Session, now: datetime) -> list[dict[str, Any]]:
    rows = s.scalars(select(Decision).where(Decision.status == "pending").order_by(Decision.created_at.desc())).all()
    return [
        {
            "id": d.id,
            "symbol": d.symbol,
            "name": d.name,
            "side": d.side,
            "qty": d.qty,
            "price": d.price,
            "amount": round(d.qty * d.price),
            "pnl": round(d.qty * (d.price - d.avg_price)) if d.side == "sell" and d.avg_price else None,
            "confidence": round(d.confidence * 100),
            "summary": d.summary,
            "strategy": d.strategy_label,
            "expires_at": iso_kst(d.expires_at),
        }
        for d in rows
    ]


def dashboard(c: Container) -> dict[str, Any]:
    now = now_kst()
    acct, error = _account(c)
    with session_scope(c.session_factory) as s:
        st = c.engine.account_settings(s)
        market = c.market.status(now)
        base = {
            **meta(c),
            "clock": {
                "now": iso_kst(now),
                "date_label": f"{now:%Y.%m.%d} ({WEEKDAYS[now.weekday()]})",
                "last_sync": c.engine.last_tick_at.strftime("%H:%M:%S") if c.engine.last_tick_at else None,
            },
            "market": {"open": market.open, "label": market.label},
            "onboarded": st.onboarded,
            "error": error or c.engine.last_error,
        }
        if acct is None:
            return base | {"ready": False}
        loss = c.engine.loss_state(s, st, acct.equity, now)
        day = c.engine.day_state(s, acct.equity, now)
        realized, fills = _realized_today(s, now)
        strategies = _strategy_rows(c, s, acct.equity)
        running = sum(1 for x in strategies if x["status"] == "running")
        paused = sum(1 for x in strategies if x["status"] == "paused")
        cancelled_on_stop = 0
        if not st.running:
            last_stop = s.scalar(select(Activity).where(Activity.title == "긴급 정지").order_by(Activity.at.desc()))
            if last_stop:
                cancelled_on_stop = s.scalar(
                    select(func.count()).select_from(Decision).where(Decision.status_note == "긴급 정지", Decision.decided_at >= last_stop.at - timedelta(seconds=5))
                ) or 0
        change = acct.equity - day.start_equity
        pending = _pending(s, now)
        return base | {
            "ready": True,
            "status": {
                "running": st.running,
                "mode": st.mode,
                "effective_mode": c.engine.effective_mode(st),
                "mode_label": MODE_LABEL[st.mode],
                "strategies_running": running,
                "strategies_paused": paused,
                "cancelled_on_stop": cancelled_on_stop,
                "live_locked": c.broker.is_live and not st.live_confirmed,
            },
            "loss": {
                "used": round(loss.used),
                "limit": round(loss.limit),
                "pct": round(loss.ratio * 100),
                "action": st.limit_action,
            },
            "summary": {
                "account_label": acct.account_label,
                "equity": round(acct.equity),
                "change": round(change),
                "change_pct": round(change / day.start_equity * 100, 2) if day.start_equity else 0,
                "realized_today": round(realized),
                "fills_today": fills,
                "cash": round(acct.cash),
                "cash_pct": round(acct.cash / acct.equity * 100) if acct.equity else 0,
                **_win_stats(s, now),
            },
            "strategies": strategies,
            "holdings": _holdings(c, s, acct),
            "feed": _feed(s, now),
            "pending": pending,
            "notifications": len(pending),
        }


PERIODS = {"1w": 7, "1m": 30, "3m": 91, "1y": 365}


def performance(c: Container, period: str) -> dict[str, Any]:
    days = PERIODS.get(period, 30)
    now = now_kst()
    with session_scope(c.session_factory) as s:
        snaps = s.scalars(
            select(EquitySnapshot).where(EquitySnapshot.at >= now - timedelta(days=days)).order_by(EquitySnapshot.at)
        ).all()
    if len(snaps) < 2:
        return {"period": period, "points": [], "labels": [], "ai_return": None, "bench_return": None, "excess": None}
    step = max(1, len(snaps) // 80)
    picked = snaps[::step]
    if picked[-1] is not snaps[-1]:
        picked.append(snaps[-1])
    e0 = picked[0].equity or 1
    b0 = picked[0].benchmark or 1
    points = [
        {"t": iso_kst(p.at), "ai": round((p.equity / e0 - 1) * 100, 3), "bench": round((p.benchmark / b0 - 1) * 100, 3) if p.benchmark and b0 else 0}
        for p in picked
    ]
    span = (picked[-1].at - picked[0].at).total_seconds()
    fmt = "%H:%M" if span < 86400 * 1.5 else "%m/%d"
    labels = [picked[round(i * (len(picked) - 1) / 4)].at.strftime(fmt) for i in range(5)]
    ai_ret, bench_ret = points[-1]["ai"], points[-1]["bench"]
    return {
        "period": period,
        "points": points,
        "labels": labels,
        "ai_return": round(ai_ret, 2),
        "bench_return": round(bench_ret, 2),
        "excess": round(ai_ret - bench_ret, 2),
    }


def decision_detail(c: Container, decision_id: str) -> dict[str, Any] | None:
    now = now_kst()
    acct, error = _account(c)
    with session_scope(c.session_factory) as s:
        d = s.get(Decision, decision_id)
        if d is None:
            return None
        strat = s.get(Strategy, d.strategy_id)
        pos = s.scalar(select(Position).where(Position.strategy_id == d.strategy_id, Position.symbol == d.symbol))
        order = s.scalar(select(Order).where(Order.decision_id == d.id).order_by(Order.id.desc()))
        realized, _ = _realized_today(s, now)
        invested = 0.0
        if strat:
            for p in s.scalars(select(Position).where(Position.strategy_id == strat.id)).all():
                try:
                    invested += p.qty * c.market.quote(p.symbol).price
                except Exception:  # noqa: BLE001
                    invested += p.qty * p.avg_price
        try:
            current = c.market.quote(d.symbol).price
        except Exception:  # noqa: BLE001
            current = d.price
        left = int((d.expires_at - now).total_seconds()) if d.expires_at and d.status == "pending" else None
        equity = acct.equity if acct else 0
        return {
            **meta(c),
            "error": error,
            "id": d.id,
            "symbol": d.symbol,
            "name": d.name,
            "side": d.side,
            "reason": d.reason,
            "status": d.status,
            "status_label": DECISION_STATUS.get(d.status, d.status),
            "status_note": d.status_note,
            "qty": d.qty,
            "max_qty": d.max_qty,
            "position_qty": pos.qty if pos else 0,
            "price": d.price,
            "current_price": current,
            "avg_price": d.avg_price,
            "confidence": round(d.confidence * 100),
            "summary": d.summary,
            "caution": d.caution,
            "signals": d.signals,
            "history_stat": d.history_stat,
            "chart": d.chart,
            "risk_rules": d.risk_rules,
            "risk_notes": d.risk_notes,
            "model": d.model,
            "strategy": {"id": strat.id, "name": strat.name, "label": d.strategy_label, "template": strat.template, "stop_pct": strat.stop_pct, "take_pct": strat.take_pct, "model_id": TEMPLATES[strat.template]["model_id"]} if strat else None,
            "data_at": iso_kst(d.data_at),
            "created_at": iso_kst(d.created_at),
            "decided_at": iso_kst(d.decided_at),
            "decided_by": d.decided_by,
            "feedback": d.feedback,
            "mode": d.mode,
            "expires_at": iso_kst(d.expires_at),
            "seconds_left": left,
            "timeout_seconds": c.settings.approval_timeout_seconds,
            "order": {
                "order_no": order.broker_order_no,
                "status": order.status,
                "qty": order.qty,
                "filled_qty": order.filled_qty,
                "fill_price": order.fill_price,
                "realized_pnl": order.realized_pnl,
                "message": order.message,
            }
            if order
            else None,
            "impact_base": {
                "equity": round(equity),
                "cash": round(acct.cash) if acct else 0,
                "holdings_count": len(acct.holdings) if acct else 0,
                "realized_today": round(realized),
                "strategy_invested": round(invested),
                "already_held": bool(acct and any(h.symbol == d.symbol for h in acct.holdings)),
            },
            "fee_rate": c.settings.fee_rate,
            "sell_tax_rate": c.settings.sell_tax_rate,
        }


def decisions_list(c: Container, limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
    with session_scope(c.session_factory) as s:
        q = select(Decision).order_by(Decision.created_at.desc()).limit(limit)
        if status:
            q = q.where(Decision.status == status)
        rows = s.scalars(q).all()
        orders = {o.decision_id: o for o in s.scalars(select(Order).where(Order.decision_id.in_([d.id for d in rows]))).all()} if rows else {}
        return [
            {
                "id": d.id,
                "created_at": iso_kst(d.created_at),
                "symbol": d.symbol,
                "name": d.name,
                "side": d.side,
                "qty": d.qty,
                "price": d.price,
                "confidence": round(d.confidence * 100),
                "status": d.status,
                "status_label": DECISION_STATUS.get(d.status, d.status),
                "strategy": d.strategy_label,
                "summary": d.summary,
                "model": d.model,
                "fill_price": orders[d.id].fill_price if d.id in orders else None,
                "realized_pnl": orders[d.id].realized_pnl if d.id in orders else None,
            }
            for d in rows
        ]


def report(c: Container, period: str) -> dict[str, Any]:
    days = PERIODS.get(period, 30)
    now = now_kst()
    with session_scope(c.session_factory) as s:
        snaps = s.scalars(
            select(EquitySnapshot).where(EquitySnapshot.at >= now - timedelta(days=days)).order_by(EquitySnapshot.at)
        ).all()
        sells = s.scalars(
            select(Order).where(Order.side == "sell", Order.filled_at >= now - timedelta(days=days), Order.realized_pnl.is_not(None))
        ).all()
        strategies = {st.id: st.name for st in s.scalars(select(Strategy)).all()}
        by_strategy: dict[str, dict[str, float]] = {}
        for o in sells:
            row = by_strategy.setdefault(strategies.get(o.strategy_id, "?"), {"realized": 0.0, "trades": 0, "wins": 0})
            row["realized"] += o.realized_pnl or 0
            row["trades"] += 1
            row["wins"] += (o.realized_pnl or 0) > 0
    metrics: dict[str, Any] = {"total_return": None, "volatility": None, "max_drawdown": None, "sharpe": None, "bench_return": None}
    if len(snaps) >= 2:
        eq = pd.Series([x.equity for x in snaps], index=pd.DatetimeIndex([x.at for x in snaps]))
        daily = eq.resample("D").last().dropna()
        bench = pd.Series([x.benchmark for x in snaps], index=eq.index)
        metrics["total_return"] = round((eq.iloc[-1] / eq.iloc[0] - 1) * 100, 2)
        metrics["max_drawdown"] = round(float(ts.max_drawdown(eq).min()) * 100, 2)
        if bench.iloc[0]:
            metrics["bench_return"] = round((bench.iloc[-1] / bench.iloc[0] - 1) * 100, 2)
        if len(daily) >= 5:
            metrics["volatility"] = round(float(ts.returns(daily).std() * math.sqrt(252) * 100), 1)
            metrics["sharpe"] = round(ts.sharpe_ratio(daily), 2)
    pnls = [o.realized_pnl or 0 for o in sells]
    return {
        "period": period,
        "metrics": metrics,
        "trades": {
            "count": len(pnls),
            "wins": sum(1 for p in pnls if p > 0),
            "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1) if pnls else None,
            "realized": round(sum(pnls)),
            "best": round(max(pnls)) if pnls else None,
            "worst": round(min(pnls)) if pnls else None,
            "avg": round(sum(pnls) / len(pnls)) if pnls else None,
        },
        "by_strategy": [{"name": k, **{kk: round(v) if kk == "realized" else v for kk, v in row.items()}} for k, row in by_strategy.items()],
        "performance": performance(c, period),
    }


def settings_view(c: Container) -> dict[str, Any]:
    now = now_kst()
    acct, error = _account(c)
    with session_scope(c.session_factory) as s:
        st: AccountSettings = c.engine.account_settings(s)
        paper_days = (now - st.paper_started_at).days if st.paper_started_at else 0
        has_strategy = bool(s.scalar(select(func.count()).select_from(Strategy)))
        return {
            **meta(c),
            "error": error,
            "account_label": acct.account_label if acct else None,
            "cash": round(acct.cash) if acct else None,
            "equity": round(acct.equity) if acct else None,
            "onboarded": st.onboarded,
            "risk_profile": st.risk_profile,
            "invest_amount": st.invest_amount,
            "daily_loss_limit": st.daily_loss_limit,
            "limit_action": st.limit_action,
            "agreed": st.agreed,
            "mode": st.mode,
            "running": st.running,
            "paper_started_at": iso_kst(st.paper_started_at),
            "paper_days": paper_days,
            "paper_days_required": c.settings.paper_days_before_live,
            "live_confirmed": st.live_confirmed,
            "live_unlocked": paper_days >= c.settings.paper_days_before_live,
            "has_strategy": has_strategy,
        }
