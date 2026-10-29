"""백로그 P1: 평균회귀 병행과 약세장 국면 배분 (모멘텀 → 평균회귀), 최소 포트폴리오 백테스트."""

import pandas as pd

from app.api.routes import StrategyIn, create_strategy
from app.db import now_kst
from app.quant.allocation import regime_allocations
from app.quant.backtest import Sleeve, run_portfolio_backtest
from app.quant.signals import Candidate, Signal, indicators


def test_regime_allocations_move_momentum_share_to_meanrev_keeping_total():
    items = [(1, "momentum", 7_000_000), (2, "meanrev", 3_000_000)]
    assert regime_allocations(items, False, 0.3) == {1: 7_000_000, 2: 3_000_000}
    weak = regime_allocations(items, True, 0.3)
    assert round(weak[1]) == 4_900_000 and round(weak[2]) == 5_100_000 and round(sum(weak.values())) == 10_000_000
    assert regime_allocations(items, True, 0.0) == {1: 7_000_000, 2: 3_000_000}
    # 받을 평균회귀가 없으면 옮기지 않습니다 (모멘텀은 이미 약세장에서 종목당 금액을 줄임).
    assert regime_allocations([(1, "momentum", 7_000_000), (3, "value", 1_000_000)], True, 0.3) == {1: 7_000_000, 3: 1_000_000}
    two = regime_allocations([(1, "momentum", 6_000_000), (2, "meanrev", 1_000_000), (4, "meanrev", 3_000_000)], True, 0.5)
    assert round(two[2]) == 1_750_000 and round(two[4]) == 5_250_000
    assert regime_allocations(items, True, 2.0)[1] == 0.0  # 1 을 넘는 값은 1 로 자릅니다


def _two_strategies(c):
    create_strategy(c, StrategyIn(template="momentum", universe="custom", custom_symbols=["005930"], amount=7_000_000, stop_pct=5, take_pct=10, max_weight_pct=15))
    create_strategy(c, StrategyIn(template="meanrev", universe="custom", custom_symbols=["000660"], amount=3_000_000, stop_pct=5, take_pct=10, max_weight_pct=15))


def _buys(c, weak, monkeypatch):
    monkeypatch.setattr(c.engine, "_bench", lambda: (0.0, 15.0, weak))
    return {p.candidate.symbol: p for p in c.engine._prepare(now_kst()) if p.candidate.side == "buy"}  # noqa: SLF001


def test_weak_market_shifts_allocation_from_momentum_to_meanrev(container, monkeypatch):
    c = container
    _two_strategies(c)
    monkeypatch.setattr(
        "app.trading.engine.describe_entry",
        lambda template, symbol, name, ind, bench: Candidate(symbol, name, "buy", "breakout" if template == "momentum" else "meanrev_entry", 0.6, [Signal("x", "테스트", "테스트", "buy", 0.6)]),
    )
    calm, weak = _buys(c, False, monkeypatch), _buys(c, True, monkeypatch)
    spent = lambda p: p.qty * p.price  # noqa: E731
    # 평균회귀: 3,000,000 × 15% → 약세장 5,100,000 × 15%
    assert spent(calm["000660"]) <= 450_000 < spent(weak["000660"]) <= 765_000
    assert any("국면 배분" in n and "5,100,000원" in n for n in weak["000660"].risk_notes)
    assert weak["000660"].context["strategy"]["effective_allocation"] == 5_100_000
    # 모멘텀: 7,000,000 × 15% → 약세장 4,900,000 × 15% × 종목당 50%
    assert spent(weak["005930"]) <= 4_900_000 * 0.15 * 0.5 < spent(calm["005930"])
    assert not any("국면 배분" in n for n in calm["000660"].risk_notes)
    c.settings.regime_weak_shift = 0.0
    assert not any("국면 배분" in n for n in _buys(c, True, monkeypatch)["000660"].risk_notes)


def test_portfolio_backtest_shares_cash_and_applies_regime_shift():
    closes = [90 + i * 10 / 79 for i in range(80)] + [102.0]
    idx = pd.bdate_range(end="2026-10-29", periods=len(closes))
    ind = indicators(pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1000.0] * 80 + [3000.0]}, index=idx))
    weak_bench = pd.Series([100 + i * 10 / 79 for i in range(80)] + [95.0], index=idx)  # 마지막 날 60일선 아래
    sleeves = [Sleeve("momentum", 0.7, 5, 10, 15), Sleeve("meanrev", 0.3, 5, 10, 15)]
    assert run_portfolio_backtest(sleeves, {"A": ind}, weak_bench, regime_shift=0.0, years=1).orders == 1
    # 모멘텀 몫을 전부 평균회귀로 옮기면 모멘텀은 살 배분이 없습니다.
    assert run_portfolio_backtest(sleeves, {"A": ind}, weak_bench, regime_shift=1.0, years=1).orders == 0
    assert not run_portfolio_backtest([Sleeve("value", 1.0, 5, 10, 15)], {"A": ind}, weak_bench).ok
