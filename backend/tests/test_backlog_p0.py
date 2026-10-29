"""백로그 P0: 리서치 대상(돌파 임박), KRX 휴장일, 전략 배분 수정, 운영 점검(체결가 vs 종가·ML 반영 여부)."""

import json
from datetime import date, datetime, timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.routes import StrategyIn, create_strategy
from app.db import now_kst, session_scope
from app.insights import Insights
from app.kiwoom.market import KiwoomMarket
from app.main import create_app
from app.market import calendar
from app.market.base import MarketStatus, krx_session_status
from app.models import Activity, LedgerEntry, Order, Position, Strategy
from app.ops import fill_vs_close, ml_review, summarize
from app.quant.signals import Candidate, Signal
from app.research.daily import pick_symbols
from app.trading.engine import EngineError
from tests.test_engine import add_decision, add_strategy

# ---------------------------------------------------------------------------- 휴장일


def test_krx_holidays_close_the_session_and_fail_closed_after_coverage():
    at = lambda d, h=10: krx_session_status(datetime(d.year, d.month, d.day, h, 0))  # noqa: E731
    assert at(date(2026, 10, 29)).open
    for d, name in ((date(2026, 12, 25), "성탄절"), (date(2026, 12, 31), "연말"), (date(2027, 2, 9), "설날"), (date(2027, 12, 27), "성탄절")):
        status = at(d)
        assert not status.open and name in status.label
    # 목록이 끝난 뒤의 평일은 갱신 전까지 닫힌 것으로 봅니다.
    assert calendar.COVERED_THROUGH == date(2027, 12, 31)
    assert not at(date(2028, 1, 4)).open and "범위 밖" in at(date(2028, 1, 4)).label
    assert not calendar.is_trading_day(date(2026, 10, 31))  # 토요일


def test_extra_holidays_from_settings():
    try:
        calendar.set_extra_holidays("2026-11-02, bad-date")
        assert not krx_session_status(datetime(2026, 11, 2, 10, 0)).open
        assert krx_session_status(datetime(2026, 11, 3, 10, 0)).open
    finally:
        calendar.set_extra_holidays("")
    assert krx_session_status(datetime(2026, 11, 2, 10, 0)).open


def test_engine_skips_holidays_and_refuses_orders(container, monkeypatch):
    c = container
    add_strategy(c, template="momentum")
    c.market.always_open = False
    monkeypatch.setattr(
        "app.trading.engine.describe_entry",
        lambda template, symbol, name, ind, bench: Candidate(symbol, name, "buy", "breakout", 0.6, [Signal("breakout", "돌파", "테스트", "buy", 0.6)]),
    )
    asked: list[str] = []
    real = c.market.history
    monkeypatch.setattr(c.market, "history", lambda sym, days=800: asked.append(sym) or real(sym, days))
    christmas = datetime(2026, 12, 25, 15, 5)
    c.engine._prefetch(christmas)  # noqa: SLF001
    assert c.engine._prepare(christmas) == [] and asked == []  # noqa: SLF001
    assert [p for p in c.engine._prepare(datetime(2026, 12, 24, 15, 5)) if p.candidate.side == "buy"]  # noqa: SLF001
    # 휴장일·장외에는 사용자가 승인해도 주문을 내지 않습니다.
    c.market.always_open = True
    did = add_decision(c, add_strategy(c))
    monkeypatch.setattr(c.market, "status", lambda now: MarketStatus(False, "휴장일 (성탄절) · 10:00"))
    with pytest.raises(EngineError, match="장이 열려 있지 않아"):
        c.engine.approve(did)


def test_daily_bar_cache_treats_holiday_as_settled():
    assert KiwoomMarket._bar_phase(datetime(2026, 12, 25, 11, 0)) == "settled"  # noqa: SLF001
    assert KiwoomMarket._bar_phase(datetime(2026, 12, 24, 11, 0)) == "live"  # noqa: SLF001


# ---------------------------------------------------------------------------- 리서치 대상


class _FakeMarket:
    def __init__(self, frames: dict[str, pd.DataFrame], risky: set[str]) -> None:
        self.frames, self.risky = frames, risky

    def history(self, symbol: str, days: int = 800) -> pd.DataFrame:
        return self.frames[symbol].iloc[-days:]

    def risky_symbols(self) -> set[str]:
        return self.risky


def _bars(closes: list[float]) -> pd.DataFrame:
    idx = pd.bdate_range(end="2026-10-28", periods=len(closes))
    return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1000.0] * len(closes)}, index=idx)


def test_research_picks_holdings_then_breakout_watchlist_not_ml(container, settings):
    c = container
    near = [80 + i * 20 / 99 for i in range(100)]  # 계속 올라 어제가 20일 최고
    frames = {
        "AAA": _bars(near),
        "BBB": _bars([80 + i * 30 / 89 for i in range(90)] + [99.0] * 10),  # 20일 고점 110 대비 −10%
        "CCC": _bars([150 - i * 50 / 79 for i in range(80)] + [100.0] * 20),  # 고점 근처지만 60일선 아래
        "DDD": _bars(near),  # 조건은 맞지만 투자유의
        "EEE": _bars(near[:-1] + [near[-2] * 0.99]),  # 고점 대비 −1% 안쪽
    }
    sid = create_strategy(c, StrategyIn(template="momentum", universe="custom", custom_symbols=list(frames), amount=1_000_000, stop_pct=5, take_pct=10, max_weight_pct=15))["id"]
    with session_scope(c.session_factory) as s:
        s.add(Position(strategy_id=sid, symbol="ZZZ", name="보유", qty=1, avg_price=100, opened_at=now_kst()))
    # 전날 ML 상위 종목은 더 이상 대상이 아닙니다.
    (settings.data_dir / "ml").mkdir(parents=True)
    (settings.data_dir / "ml" / "latest.json").write_text(json.dumps({"as_of": "2026-10-28", "scores": {"BBB": {"rank": 1, "pct": 0.0}}}))
    picked = pick_symbols(settings, 5, _FakeMarket(frames, {"DDD"}))
    assert picked == ["ZZZ", "AAA", "EEE"]
    assert pick_symbols(settings, 2, _FakeMarket(frames, {"DDD"})) == ["ZZZ", "AAA"]


# ---------------------------------------------------------------------------- 배분 수정


def test_patch_strategy_allocation_without_recreating(settings, container):
    c = container
    sid = add_strategy(c, template="momentum", amount=10_000_000)
    equity = c.engine.account().equity
    with TestClient(create_app(settings, c)) as http:
        res = http.patch(f"/api/strategies/{sid}", json={"allocation": 7_000_000})
        assert res.status_code == 200 and res.json()["allocation"] == 7_000_000 and res.json()["status"] == "running"
        other = add_strategy(c, template="meanrev", amount=3_000_000)
        too_much = http.patch(f"/api/strategies/{other}", json={"allocation": int(equity)})
        assert too_much.status_code == 400 and "평가자산" in too_much.json()["detail"]
        assert http.patch(f"/api/strategies/{sid}", json={}).status_code == 400
        assert http.patch(f"/api/strategies/{sid}", json={"status": "paused"}).json()["status"] == "paused"
    with session_scope(c.session_factory) as s:
        assert s.get(Strategy, sid).allocation == 7_000_000
        assert s.scalar(select(Activity).where(Activity.title.like("%배분 변경"))).detail.startswith("10,000,000원 → 7,000,000원")
        assert s.scalar(select(LedgerEntry).where(LedgerEntry.kind == "strategy.allocation")).payload == {"from": 10_000_000, "to": 7_000_000}


def test_lower_allocation_limits_new_buys(container, monkeypatch):
    c = container
    sid = create_strategy(c, StrategyIn(template="momentum", universe="custom", custom_symbols=["005930"], amount=10_000_000, stop_pct=5, take_pct=10, max_weight_pct=15))["id"]
    monkeypatch.setattr(
        "app.trading.engine.describe_entry",
        lambda template, symbol, name, ind, bench: Candidate(symbol, name, "buy", "breakout", 0.6, [Signal("breakout", "돌파", "테스트", "buy", 0.6)]),
    )
    monkeypatch.setattr(c.engine, "_bench", lambda: (0.0, 15.0, False))
    big = next(p for p in c.engine._prepare(now_kst()) if p.candidate.side == "buy")  # noqa: SLF001
    with session_scope(c.session_factory) as s:
        s.get(Strategy, sid).allocation = 2_000_000
    small = next(p for p in c.engine._prepare(now_kst()) if p.candidate.side == "buy")  # noqa: SLF001
    assert small.qty * small.price <= 2_000_000 * 0.15 < big.qty * big.price


# ---------------------------------------------------------------------------- 운영 점검


def test_fill_vs_close_measures_cost_against_daily_close(container):
    c = container
    sid = add_strategy(c)
    close = c.market.history("005930", 60)["close"]
    day = close.index[-5]
    at = day.to_pydatetime() + timedelta(hours=15, minutes=10)
    with session_scope(c.session_factory) as s:
        for side, mult, no in (("buy", 1.01, "B1"), ("sell", 0.995, "S1")):
            s.add(Order(strategy_id=sid, broker_order_no=no, symbol="005930", name="삼성전자", side=side, qty=10, filled_qty=10, fill_price=float(close.loc[day]) * mult, status="filled", created_at=at, filled_at=at))
        rows = fill_vs_close(s, c.market, at - timedelta(days=1), now_kst())
    assert [(r["side"], r["cost_pct"]) for r in rows] == [("buy", 1.0), ("sell", 0.5)]
    summary = summarize(rows, 0.0005)
    assert summary["avg_cost_pct"] == 0.75 and summary["buy"]["count"] == 1 and summary["assumed_pct"] == 0.05


def test_ml_review_uses_engine_gate(tmp_path):
    today = now_kst().date()
    (tmp_path / "ml").mkdir()
    path = tmp_path / "ml" / "latest.json"
    path.write_text(json.dumps({"as_of": today.isoformat(), "model": "m", "scores": {"A": {}}, "metrics": {"valid_rank_ic": 0.04, "fold_rank_ics": [0.05, 0.03, -0.01], "random_null": {"alpha_t": 2.5}}}))
    assert ml_review(Insights(tmp_path), today)["ok"]
    path.write_text(json.dumps({"as_of": today.isoformat(), "model": "m", "scores": {"A": {}}, "metrics": {"valid_rank_ic": 0.04, "fold_rank_ics": [0.12, -0.01, -0.02]}}))
    review = ml_review(Insights(tmp_path), today)
    assert not review["ok"] and "1개만 양수" in review["why"]
    path.write_text(json.dumps({"as_of": (today - timedelta(days=10)).isoformat(), "model": "m", "scores": {}, "metrics": {"valid_rank_ic": 0.05}}))
    assert "오래돼" in ml_review(Insights(tmp_path), today)["why"]
    assert not ml_review(Insights(tmp_path / "none"), today)["ok"]
