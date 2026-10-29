import json
from datetime import timedelta

from sqlalchemy import select

from app import ledger
from app.db import now_kst, session_scope
from app.insights import Insights
from app.models import Decision, EquitySnapshot, LedgerEntry, Order
from app.trading.protections import ProtectionConfig, account_block, symbol_block
from tests.test_engine import add_decision, add_strategy


def test_stop_loss_blocks_reentry_and_guard_counts(container):
    c = container
    sid = add_strategy(c)
    c.engine.approve(add_decision(c, sid, qty=10))
    c.engine.approve(add_decision(c, sid, side="sell", qty=10, reason="stop_loss"))
    cfg = ProtectionConfig(stoploss_guard_count=1)
    with session_scope(c.session_factory) as s:
        assert "손절 후 재진입 금지" in symbol_block(s, cfg, "005930", now_kst())
        assert symbol_block(s, cfg, "000660", now_kst()) is None
        assert "손절 연속 가드" in account_block(s, cfg, now_kst())
        # 3거래일이 지나면 풀립니다
        assert symbol_block(s, cfg, "005930", now_kst() + timedelta(days=7)) is None


def test_max_drawdown_guard(container):
    c = container
    now = now_kst()
    with session_scope(c.session_factory) as s:
        s.add(EquitySnapshot(at=now - timedelta(days=3), equity=20_000_000, cash=0, benchmark=1))
        s.add(EquitySnapshot(at=now, equity=17_500_000, cash=0, benchmark=1))
    with session_scope(c.session_factory) as s:
        assert "최대 낙폭 가드" in account_block(s, ProtectionConfig(max_drawdown_pct=10), now)
        assert account_block(s, ProtectionConfig(max_drawdown_pct=15), now) is None


def test_ledger_chain_records_lifecycle_and_detects_tampering(container):
    c = container
    sid = add_strategy(c)
    did = add_decision(c, sid, qty=3)
    c.engine.approve(did)
    c.engine.set_mode("notify")
    c.engine.stop()
    with session_scope(c.session_factory) as s:
        kinds = [e["kind"] for e in ledger.entries_for(s, did)]
        assert kinds == ["decision.approved", "order.submitted", "order.filled"]
        result = ledger.verify(s)
        assert result["ok"] and result["count"] >= 5
        # 과거 기록 하나를 고치면 검증이 깨집니다.
        first = s.scalar(select(LedgerEntry).where(LedgerEntry.kind == "decision.approved"))
        first.payload = {**first.payload, "qty": 300}
    with session_scope(c.session_factory) as s:
        broken = ledger.verify(s)
        assert not broken["ok"] and broken["broken_at"] == 1


def test_research_and_ml_add_signals(container, tmp_path):
    c = container
    today = now_kst().strftime("%Y-%m-%d")
    (tmp_path / "ml").mkdir()
    (tmp_path / "research").mkdir()
    syms = ["005930", "000660", "373220"]
    (tmp_path / "ml" / "latest.json").write_text(json.dumps({"as_of": today, "model": "qlib-lgbm", "metrics": {"valid_rank_ic": 0.05, "random_null": {"alpha_t": 3.0}}, "scores": {s: {"score": 0.1 - i * 0.05, "rank": i + 1, "pct": i / 2} for i, s in enumerate(syms)}}))
    (tmp_path / "research" / "latest.json").write_text(json.dumps({"as_of": today, "items": {"000660": {"rating": "Sell", "as_of": today, "summary": "과열", "report": "x.md"}}}))
    ins = Insights(tmp_path)
    sig, adjust = ins.ml_signal("005930", "buy", now_kst().date())
    assert sig.side == "buy" and adjust > 0 and "1위" in sig.detail
    assert ins.ml_signal("373220", "buy", now_kst().date())[0].side == "hold"
    assert ins.research_signal("000660", now_kst().date()).side == "sell"
    # 검증 예측력이 낮으면 참고만 하고 점수는 바꾸지 않습니다
    weak = Insights(tmp_path, min_rank_ic=0.1)
    sig_w, adj_w = weak.ml_signal("005930", "buy", now_kst().date())
    assert sig_w.side == "neutral" and adj_w == 0.0 and "참고만" in sig_w.detail

    c.engine.insights = ins
    c.settings.enable_value_strategy = True  # 가치 전략은 기본 꺼짐
    add_strategy(c, template="value")
    c.engine.tick()
    with session_scope(c.session_factory) as s:
        # 약세 리서치는 매수를 막지 않고 금액만 줄입니다 (자세한 검사는 test_strategy_rules).
        for d in s.scalars(select(Decision).where(Decision.symbol == "000660", Decision.side == "buy")).all():
            assert any("심층 리서치 의견" in n for n in d.risk_notes)
        for d in s.scalars(select(Decision).where(Decision.symbol.in_(["005930", "373220"]))).all():
            assert any(x["key"] == "ml" for x in d.signals)
