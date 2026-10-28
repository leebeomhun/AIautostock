from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.ai.analyst import AIAnalyst, Judgment, SignalOut
from app.api.routes import StrategyIn, create_strategy
from app.db import now_kst, session_scope
from app.models import AccountSettings, Activity, DayState, Decision, Order, Position, Strategy
from app.quant.signals import Candidate, Signal
from app.trading.engine import EngineError


def add_strategy(c, template="meanrev", exec_mode="approve", amount=10_000_000):
    return create_strategy(c, StrategyIn(template=template, universe="k200", amount=amount, stop_pct=5, take_pct=10, max_weight_pct=15, exec_mode=exec_mode))["id"]


def add_decision(c, strategy_id, symbol="005930", side="buy", qty=10, reason="breakout", expires_in=300, avg=None):
    now = now_kst()
    price = c.market.quote(symbol).price
    with session_scope(c.session_factory) as s:
        n = len(s.scalars(select(Decision)).all())
        d = Decision(
            id=f"D-TEST-{n + 1:04d}", strategy_id=strategy_id, symbol=symbol, name="테스트", side=side, reason=reason,
            qty=qty, max_qty=qty, price=price, avg_price=avg, confidence=0.7, summary="테스트 판단", signals=[],
            risk_rules=[], risk_notes=[], model="rule-based", strategy_label="테스트 v1", data_at=now, mode="approve",
            status="pending", expires_at=now + timedelta(seconds=expires_in), created_at=now,
        )
        s.add(d)
        return d.id


def position(c, strategy_id, symbol):
    with session_scope(c.session_factory) as s:
        return s.scalar(select(Position).where(Position.strategy_id == strategy_id, Position.symbol == symbol))


def test_approve_buy_then_sell_records_realized_pnl(container):
    c = container
    sid = add_strategy(c)
    cash0 = c.engine.account().cash
    c.engine.approve(add_decision(c, sid, qty=10))
    pos = position(c, sid, "005930")
    assert pos.qty == 10
    assert c.engine.account().cash < cash0

    c.engine.approve(add_decision(c, sid, side="sell", qty=10, reason="stop_loss", avg=pos.avg_price))
    assert position(c, sid, "005930") is None
    with session_scope(c.session_factory) as s:
        sell = s.scalar(select(Order).where(Order.side == "sell"))
        assert sell.status == "filled"
        # 같은 가격에 사고 팔았으므로 슬리피지·수수료·세금만큼 손실입니다.
        assert sell.realized_pnl < 0
        kinds = [a.kind for a in s.scalars(select(Activity)).all()]
        assert "buy_fill" in kinds and "sell_fill" in kinds


def test_reject_records_feedback_and_can_reopen(container):
    c = container
    sid = add_strategy(c)
    did = add_decision(c, sid)
    d = c.engine.reject(did, "실적 발표 전이라 보류")
    assert d.status == "rejected" and d.feedback == "실적 발표 전이라 보류"
    assert c.engine.reopen(did).status == "pending"


def test_quantity_cannot_exceed_proposal(container):
    c = container
    sid = add_strategy(c)
    with pytest.raises(EngineError):
        c.engine.approve(add_decision(c, sid, qty=5), qty=6)


def test_kill_switch_cancels_pending_and_blocks_approval(container):
    c = container
    sid = add_strategy(c)
    did = add_decision(c, sid)
    assert c.engine.stop() == 1
    with session_scope(c.session_factory) as s:
        assert s.get(Decision, did).status == "cancelled"
        assert s.get(AccountSettings, 1).running is False
    with pytest.raises(EngineError):
        c.engine.approve(add_decision(c, sid))


def test_pending_decisions_expire_on_tick(container):
    c = container
    sid = add_strategy(c)
    did = add_decision(c, sid, expires_in=-1)
    c.engine.tick()
    with session_scope(c.session_factory) as s:
        assert s.get(Decision, did).status == "expired"


def test_daily_loss_limit_blocks_buys_but_allows_stop_loss(container):
    c = container
    sid = add_strategy(c)
    c.engine.approve(add_decision(c, sid, qty=10))
    equity = c.engine.account().equity
    with session_scope(c.session_factory) as s:
        s.merge(DayState(date=now_kst().strftime("%Y-%m-%d"), start_equity=equity + 400_000, alert_level=0))
    with pytest.raises(EngineError):
        c.engine.approve(add_decision(c, sid, symbol="000660", qty=1))
    c.engine.approve(add_decision(c, sid, side="sell", qty=10, reason="stop_loss"))  # 손절은 계속

    with session_scope(c.session_factory) as s:
        s.get(AccountSettings, 1).limit_action = "all"
    with pytest.raises(EngineError):
        c.engine.approve(add_decision(c, sid, side="sell", qty=1, reason="stop_loss"))


def test_live_account_is_notify_only_until_confirmed(container):
    c = container
    c.broker.is_live = True
    with session_scope(c.session_factory) as s:
        st = s.get(AccountSettings, 1)
        assert c.engine.effective_mode(st) == "notify"
        st.live_confirmed = True
        assert c.engine.effective_mode(st) == "approve"


def test_value_rebalance_creates_pending_then_auto_executes(container):
    c = container
    add_strategy(c, template="value", exec_mode="approve")
    c.engine.tick()
    with session_scope(c.session_factory) as s:
        pending = s.scalars(select(Decision).where(Decision.status == "pending")).all()
        assert pending and all(d.side == "buy" for d in pending)
        assert all(d.expires_at > now_kst() for d in pending)

    c.engine.stop()
    c.engine.start()
    c.engine.set_mode("auto")
    sid = add_strategy(c, template="value", exec_mode="auto", amount=5_000_000)
    c.engine.tick()
    with session_scope(c.session_factory) as s:
        auto_orders = s.scalars(select(Order).where(Order.strategy_id == sid)).all()
        assert auto_orders and all(o.status == "filled" for o in auto_orders)
        strat = s.get(Strategy, sid)
        assert strat.last_rebalanced is not None


class FakeResponses:
    def __init__(self, parsed):
        self.parsed = parsed

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(output_parsed=self.parsed, output=[])


def analyst_with(parsed) -> AIAnalyst:
    a = AIAnalyst(None, "gpt-test")
    a._client = SimpleNamespace(responses=FakeResponses(parsed))
    return a


def candidate(side="buy", mandatory=False):
    return Candidate("005930", "삼성전자", side, "breakout" if side == "buy" else "stop_loss", 0.6, [Signal("volume", "거래량", "2.4배", "buy", 0.8)], mandatory=mandatory)


def test_ai_cannot_flip_direction_or_add_signals():
    parsed = Judgment(
        action="sell", quantity_ratio=1.7, confidence=0.99, summary="요약", caution="",
        signals=[SignalOut(key="volume", name="거래량", detail="2.4배", side="buy", strength=2), SignalOut(key="rumor", name="소문", detail="지어낸 근거", side="buy", strength=1)],
    )
    ai = analyst_with(parsed).judge(candidate("buy"), {"proposed_qty": 10}, 5, 10)
    assert ai.action == "buy"
    assert ai.quantity_ratio == 1.0
    assert ai.confidence == 0.9
    assert [s["key"] for s in ai.signals] == ["volume"]
    assert ai.signals[0]["strength"] == 1.0


def test_ai_cannot_hold_mandatory_stop_loss():
    parsed = Judgment(action="hold", quantity_ratio=0.2, confidence=0.5, summary="기다리자", caution="", signals=[])
    a = analyst_with(parsed)
    ai = a.judge(candidate("sell", mandatory=True), {"proposed_qty": 30}, 5, 10)
    assert ai.action == "sell" and ai.quantity_ratio == 1.0
    assert a._client.responses.kwargs["text_format"] is Judgment


def test_ai_failure_falls_back_to_rules():
    a = AIAnalyst(None, "gpt-test")

    class Boom:
        def parse(self, **_):
            raise RuntimeError("timeout")

    a._client = SimpleNamespace(responses=Boom())
    ai = a.judge(candidate("buy"), {}, 5, 10)
    assert not ai.used_ai and ai.error == "timeout" and ai.action == "buy"
