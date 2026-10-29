"""모멘텀 재설계 (2026-10-29): 약세장(KOSPI 60일선 아래) 모멘텀 신규 진입 중단, 국면 배분은 평균회귀로 옮기지 않고 현금."""

from datetime import timedelta

import pandas as pd
from sqlalchemy import select

from app.api.routes import StrategyIn, create_strategy
from app.config import Settings
from app.db import now_kst, session_scope
from app.models import Activity, Position
from app.quant import signals
from app.quant.backtest import BacktestParams, run_backtest
from app.quant.signals import Candidate, Signal, indicators


def test_defaults_pause_momentum_and_do_not_shift_to_meanrev():
    assert signals.WEAK_MARKET_SIZE == {"momentum": 0.0}
    assert Settings(_env_file=None).regime_weak_shift == 0.0


def _breakout(template, symbol, name, ind, bench):
    return Candidate(symbol, name, "buy", "breakout", 0.6, [Signal("breakout", "돌파", "테스트", "buy", 0.6)])


def test_engine_weak_market_stops_new_momentum_buys_but_keeps_exits(container, monkeypatch):
    c = container
    sid = create_strategy(c, StrategyIn(template="momentum", universe="custom", custom_symbols=["005930", "000660"], amount=10_000_000, stop_pct=5, take_pct=10, max_weight_pct=15))["id"]
    # 이미 들고 있는 종목: 평균단가보다 10% 아래라 손절 대상
    with session_scope(c.session_factory) as s:
        s.add(Position(strategy_id=sid, symbol="000660", name="SK하이닉스", qty=5, avg_price=c.market.quote("000660").price / 0.9, opened_at=now_kst() - timedelta(days=3)))
    monkeypatch.setattr("app.trading.engine.describe_entry", _breakout)
    monkeypatch.setattr(c.engine, "_bench", lambda: (-0.01, 20.0, True))
    prepared = c.engine._prepare(now_kst())  # noqa: SLF001
    assert not [p for p in prepared if p.candidate.side == "buy"]  # 신규 매수도, 보유 종목 추가 매수도 없음
    exits = [p for p in prepared if p.candidate.side == "sell"]
    assert [(p.candidate.symbol, p.candidate.reason) for p in exits] == [("000660", "stop_loss")]
    with session_scope(c.session_factory) as s:
        note = s.scalar(select(Activity).where(Activity.title.like("%신규 매수 쉼")))
        assert note is not None and "60일 이동평균 아래" in note.detail
    monkeypatch.setattr(c.engine, "_bench", lambda: (0.01, 20.0, False))
    assert {p.candidate.symbol for p in c.engine._prepare(now_kst()) if p.candidate.side == "buy"} == {"005930"}  # noqa: SLF001


def _series(values: list[float]) -> pd.Series:
    return pd.Series(values, index=pd.bdate_range(end="2026-10-29", periods=len(values)))


def _frame(closes: list[float], breakout_at: int) -> pd.DataFrame:
    idx = pd.bdate_range(end="2026-10-29", periods=len(closes))
    volume = [3000.0 if k == breakout_at else 1000.0 for k in range(len(closes))]
    return indicators(pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": volume}, index=idx))


def _params() -> BacktestParams:
    return BacktestParams(template="momentum", amount=10_000_000, stop_pct=5, take_pct=10, max_weight_pct=15, years=1)


def test_backtest_skips_momentum_entries_in_weak_market():
    closes = [90 + i * 10 / 79 for i in range(80)] + [102.0]
    ind = _frame(closes, 80)
    calm = _series([100 + i * 0.1 for i in range(81)])
    weak = _series([100 + i * 0.1 for i in range(80)] + [90.0])  # 마지막 날 60일선 아래
    assert run_backtest(_params(), {"A": ind}, calm).orders == 1
    r = run_backtest(_params(), {"A": ind}, weak)
    assert r.orders == 0 and r.win_rate is None


def test_backtest_still_exits_positions_after_market_turns_weak():
    closes = [90 + i * 10 / 79 for i in range(80)] + [102.0] + [101.0, 99.0, 96.0, 94.0, 93.0]
    ind = _frame(closes, 80)
    # 돌파 날까지는 강세, 다음 날부터 60일선 아래로
    bench = _series([100 + i * 0.1 for i in range(81)] + [95.0, 94.0, 93.0, 92.0, 91.0])
    r = run_backtest(_params(), {"A": ind}, bench)
    assert r.orders == 2 and r.trades == 1 and r.win_rate == 0  # 강세 날 산 종목은 약세장에서도 손절로 청산
