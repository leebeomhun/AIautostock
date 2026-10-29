"""모멘텀 보완: 종목 범위 확대, ML·수급 가산, 트레일링 스톱, 시장 약세 비중 축소, 업종 한도."""

import json
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import select

from app.api.routes import StrategyIn, create_strategy
from app.db import now_kst, session_scope
from app.insights import Insights
from app.kiwoom.market import KiwoomMarket
from app.market import universe
from app.market.base import Instrument
from app.models import Activity, Position, Strategy
from app.quant import signals
from app.quant.backtest import BacktestParams, run_backtest
from app.quant.signals import Candidate, Signal, indicators, peak_close, trailing_exit, trailing_stop_pct, weak_market
from tests.test_engine import add_strategy
from tests.test_kiwoom import FakeKiwoom, client_for


def _frame(closes: list[float], volumes: list[float], end: str = "2026-10-29") -> pd.DataFrame:
    idx = pd.bdate_range(end=end, periods=len(closes))
    return indicators(pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": volumes}, index=idx))


def _base(n: int = 80) -> list[float]:
    return [90 + i * 10 / (n - 1) for i in range(n)]


def _bench(index: pd.DatetimeIndex, start: float = 100, end: float = 110) -> pd.Series:
    return pd.Series([start + (end - start) * i / (len(index) - 1) for i in range(len(index))], index=index)


def _momentum(c, symbols: list[str]) -> int:
    body = StrategyIn(template="momentum", universe="custom", custom_symbols=symbols, amount=10_000_000, stop_pct=5, take_pct=10, max_weight_pct=15)
    return create_strategy(c, body)["id"]


def _always_breakout(template, symbol, name, ind, bench):
    return Candidate(symbol, name, "buy", "breakout", 0.6, [Signal("breakout", "돌파", "테스트", "buy", 0.6), Signal("news", "뉴스", "없음", "neutral", 0.0)])


def _buys(c, now=None):
    return {p.candidate.symbol: p for p in c.engine._prepare(now or now_kst()) if p.candidate.side == "buy"}  # noqa: SLF001


# ---------------------------------------------------------------------------- 규칙 단위


def test_trailing_stop_is_wide_and_only_after_partial_take_profit():
    assert trailing_stop_pct("meanrev", 5, 30.0) is None
    assert trailing_stop_pct("momentum", 5, 10.0) == 10  # 변동성이 낮아도 손절폭의 2배
    assert trailing_stop_pct("momentum", 5, 80.0) == 15  # 3배를 넘지 않습니다
    assert 12 < trailing_stop_pct("momentum", 5, 50.0) < 13
    assert trailing_stop_pct("momentum", 5, None) == 10
    assert trailing_exit(10, False, 80, 100) is None
    assert trailing_exit(10, True, 91, 100) is None
    assert trailing_exit(10, True, 90, 100) == "trailing_stop"
    ind = _frame([100, 120, 110], [1, 1, 1])
    assert peak_close(ind, ind.index[1].to_pydatetime(), 105) == 120
    assert peak_close(ind, ind.index[1].to_pydatetime(), 130) == 130


def test_weak_market_is_close_below_60_day_average():
    idx = pd.bdate_range(end="2026-10-29", periods=80)
    up = _bench(idx)
    assert not weak_market(up).iloc[-1] and not weak_market(up).iloc[:59].any()  # 60일이 차기 전엔 약세로 보지 않음
    down = pd.Series(list(up.iloc[:79]) + [95.0], index=idx)
    assert weak_market(down).iloc[-1]


def test_ml_top_rank_gets_extra_buy_bonus_only_when_validated(tmp_path):
    today = now_kst().strftime("%Y-%m-%d")
    (tmp_path / "ml").mkdir()
    scores = {"A": {"score": 1, "rank": 1, "pct": 0.1}, "B": {"score": 0, "rank": 5, "pct": 0.5}}

    def write(ics):
        metrics = {"valid_rank_ic": sum(ics) / len(ics), "fold_rank_ics": ics, "label_horizon": 5, "random_null": {"alpha_t": 3.0}}
        (tmp_path / "ml" / "latest.json").write_text(json.dumps({"as_of": today, "model": "m", "metrics": metrics, "scores": scores}))

    write([0.05, 0.04, 0.03])
    ins = Insights(tmp_path)
    assert abs(ins.ml_signal("A", "buy", now_kst().date())[1] - 0.09) < 1e-9
    assert ins.ml_signal("B", "buy", now_kst().date())[1] == 0.0
    assert abs(ins.ml_signal("A", "sell", now_kst().date())[1] + 0.04) < 1e-9  # 매도 쪽에는 상위 가산이 없습니다
    write([0.01, 0.0, 0.0])
    assert Insights(tmp_path).ml_signal("A", "buy", now_kst().date())[1] == 0.0


def test_flow_signal_bonus_needs_both_institution_and_foreign_buying():
    sig, bonus = signals.flow_signal(4, 2)
    assert bonus == signals.FLOW_BONUS and sig.side == "buy" and "기관 4일 연속 순매수" in sig.detail
    assert signals.flow_signal(5, 1)[1] == 0.0
    sig, bonus = signals.flow_signal(-2, -3)
    assert bonus == 0.0 and sig.side == "hold" and "연속 순매도" in sig.detail


# ---------------------------------------------------------------------------- 백테스트


def _trailing_case() -> tuple[pd.DataFrame, pd.Series]:
    # 천천히 오르다 거래량 3배로 돌파 → +10%에서 절반 익절 → 150까지 오른 뒤 126으로 −16% (20일선 위)
    closes = _base() + [102.0] + [102 + (k + 1) * 48 / 9 for k in range(9)] + [126.0]
    volumes = [1000.0] * 80 + [3000.0] + [1000.0] * 10
    ind = _frame(closes, volumes)
    return ind, _bench(ind.index)


def test_backtest_trailing_stop_closes_remainder_after_partial(monkeypatch):
    ind, bench = _trailing_case()
    assert ind["close"].iloc[-1] > ind["ma20"].iloc[-1]  # 추세 이탈 청산이 아니라 트레일링으로 나가는지 봅니다
    params = BacktestParams(template="momentum", amount=10_000_000, stop_pct=5, take_pct=10, max_weight_pct=15, years=1)
    r = run_backtest(params, {"A": ind}, bench)
    assert (r.orders, r.trades, r.win_rate) == (3, 1, 100)  # 매수 · 절반 익절 · 트레일링 정리
    monkeypatch.setattr(signals, "TRAIL_TEMPLATES", ())
    r = run_backtest(params, {"A": ind}, bench)
    assert (r.orders, r.trades) == (2, 0)


def test_backtest_sector_limit_caps_same_sector_holdings():
    ind = _frame(_base() + [102.0], [1000.0] * 80 + [3000.0])
    frames = {s: ind for s in ("A", "B", "C")}
    sectors = {"A": "반도체", "B": "반도체", "C": "반도체"}
    base = dict(template="momentum", amount=10_000_000, stop_pct=5, take_pct=10, max_weight_pct=15, years=1, sectors=sectors)
    assert run_backtest(BacktestParams(**base), frames, _bench(ind.index)).orders == 3
    assert run_backtest(BacktestParams(**base, max_per_sector=2), frames, _bench(ind.index)).orders == 2


# ---------------------------------------------------------------------------- 엔진


def test_engine_proposes_trailing_stop_for_remainder(container, monkeypatch):
    c = container
    sid = add_strategy(c, template="momentum")
    price = c.market.quote("005930").price
    with session_scope(c.session_factory) as s:
        s.add(Position(strategy_id=sid, symbol="005930", name="삼성전자", qty=7, avg_price=price / 1.05, opened_at=now_kst() - timedelta(days=8), partial_taken=True))
        s.add(Position(strategy_id=sid, symbol="000660", name="SK하이닉스", qty=7, avg_price=c.market.quote("000660").price / 1.05, opened_at=now_kst() - timedelta(days=8)))
    monkeypatch.setattr("app.trading.engine.peak_close", lambda ind, since, p: p * 1.3)
    sells = {p.candidate.symbol: p for p in c.engine._prepare(now_kst()) if p.candidate.side == "sell"}  # noqa: SLF001
    trail = sells["005930"]
    assert trail.candidate.reason == "trailing_stop" and trail.qty == 7
    assert any(x.key == "trail" for x in trail.candidate.signals)
    assert any("트레일링" in r for r in trail.risk_rules)
    assert trail.context["position"]["peak_close"] == round(trail.price * 1.3)
    # 일부 익절 전인 종목에는 트레일링을 걸지 않습니다.
    assert "000660" not in sells or sells["000660"].candidate.reason != "trailing_stop"


def test_weak_market_pauses_momentum_entries(container, monkeypatch):
    """약세장(KOSPI 60일선 아래) 모멘텀 신규 진입 중단 — 자세한 검사는 test_momentum_redesign."""
    c = container
    _momentum(c, ["005930"])
    monkeypatch.setattr("app.trading.engine.describe_entry", _always_breakout)
    monkeypatch.setattr(c.engine, "_bench", lambda: (0.0, 15.0, False))
    assert "005930" in _buys(c)
    monkeypatch.setattr(c.engine, "_bench", lambda: (0.0, 15.0, True))
    assert signals.WEAK_MARKET_SIZE["momentum"] == 0.0 and _buys(c) == {}


def test_sector_limit_skips_extra_same_sector_buys(container, monkeypatch):
    c = container
    syms = ["005930", "000660", "403870", "005380"]  # 반도체 3 + 자동차 1
    _momentum(c, syms)
    monkeypatch.setattr("app.trading.engine.describe_entry", _always_breakout)
    assert len(_buys(c)) == 3  # 기본(0)은 제한 없이 주기당 진입 수(3)까지
    c.settings.max_positions_per_sector = 1
    assert set(_buys(c)) == {"005930", "005380"}
    with session_scope(c.session_factory) as s:
        notes = [a.detail for a in s.scalars(select(Activity).where(Activity.kind == "risk_guard")).all()]
    assert any("같은 업종(반도체)" in n for n in notes)


def test_ml_rank_reorders_entries_before_tick_cap(container, tmp_path, monkeypatch):
    c = container
    today = now_kst().strftime("%Y-%m-%d")
    (tmp_path / "ml").mkdir()
    syms = ["005930", "000660", "005380", "000270"]
    pcts = {"005930": 1.0, "000660": 0.6, "005380": 0.4, "000270": 0.0}
    scores = {s: {"score": 1 - p, "rank": int(p * 3) + 1, "pct": p} for s, p in pcts.items()}
    metrics = {"valid_rank_ic": 0.05, "fold_rank_ics": [0.05, 0.04, 0.06], "label_horizon": 5, "random_null": {"alpha_t": 3.0}}
    (tmp_path / "ml" / "latest.json").write_text(json.dumps({"as_of": today, "model": "m", "metrics": metrics, "scores": scores}))
    c.engine.insights = Insights(tmp_path)
    _momentum(c, syms)
    monkeypatch.setattr("app.trading.engine.describe_entry", _always_breakout)
    buys = _buys(c)
    # 같은 퀀트 점수라면 목록 순서로 앞 3종목이 들어가지만, ML 꼴찌(005930)가 빠지고 1위(000270)가 들어갑니다.
    assert set(buys) == {"000660", "005380", "000270"}
    assert round(buys["000270"].candidate.score, 2) == 0.7  # 0.6 + 순위 0.05 + 상위 20% 0.05


def test_flow_bonus_applies_to_momentum_candidates(container, monkeypatch):
    c = container
    _momentum(c, ["005930", "000660", "005380"])
    monkeypatch.setattr("app.trading.engine.describe_entry", _always_breakout)
    monkeypatch.setattr(c.market, "investor_flow", lambda: {"000660": (3, 2), "005380": (-2, -3)}, raising=False)
    buys = _buys(c)
    assert round(buys["000660"].candidate.score, 2) == 0.63 and round(buys["005380"].candidate.score, 2) == 0.6
    flow = {s: next((x for x in buys[s].candidate.signals if x.key == "flow"), None) for s in buys}
    assert flow["000660"].side == "buy" and flow["005380"].side == "hold" and flow["005930"] is None
    keys = [x.key for x in buys["000660"].candidate.signals]
    assert keys.index("flow") < keys.index("news")


def test_k200_universe_expands_with_market_cap_top(container, monkeypatch):
    c = container
    monkeypatch.setattr(universe, "ALL_INSTRUMENTS", dict(universe.ALL_INSTRUMENTS))
    monkeypatch.setattr(c.market, "top_by_market_cap", lambda n: ["111110", "005930"][:n], raising=False)
    monkeypatch.setattr(c.market, "listed_instruments", lambda: [Instrument("111110", "테스트전자", "KOSPI", "전기·전자")], raising=False)
    k200 = Strategy(universe="k200", custom_symbols=[])
    base = universe.universe_symbols("k200")
    assert set(c.engine._universe(k200)) == set(base) | {"111110"}  # noqa: SLF001
    assert c.engine._universe(Strategy(universe="custom", custom_symbols=["005930"])) == ["005930"]  # noqa: SLF001
    c.settings.k200_universe_size = 0
    assert c.engine._universe(k200) == base  # noqa: SLF001
    c.engine._register_listing(now_kst())  # noqa: SLF001
    assert universe.instrument("111110").name == "테스트전자" and universe.instrument("111110").sector == "전기·전자"


def test_close_window_strategies_load_only_held_symbols_outside_window(container, monkeypatch):
    c = container
    sid = add_strategy(c, template="momentum")
    with session_scope(c.session_factory) as s:
        s.add(Position(strategy_id=sid, symbol="005930", name="삼성전자", qty=3, avg_price=c.market.quote("005930").price, opened_at=now_kst() - timedelta(days=3)))
    c.market.always_open = False
    c.settings.close_window = "15:00-15:20"
    asked: list[str] = []
    real = c.market.history
    monkeypatch.setattr(c.market, "history", lambda sym, days=800: asked.append(sym) or real(sym, days))
    morning, closing = datetime(2026, 10, 28, 10, 0), datetime(2026, 10, 28, 15, 5)
    c.engine._prefetch(morning)  # noqa: SLF001
    c.engine._prepare(morning)  # noqa: SLF001
    assert [x for x in asked if not x.startswith("__")] == ["005930"]  # '__KOSPI__' 는 시뮬레이션 지수
    asked.clear()
    c.engine._prefetch(closing)  # noqa: SLF001
    assert set(asked) == set(universe.universe_symbols("k200"))  # 잠금 밖에서 미리 받아 둡니다
    asked.clear()
    c.engine._prepare(closing)  # noqa: SLF001
    assert set(asked) >= set(universe.universe_symbols("k200"))


def test_kiwoom_listing_sectors_and_investor_flow(tmp_path, monkeypatch):
    fake = FakeKiwoom()
    fake.responses["ka10099"] = [
        {"return_code": 0, "list": [{"code": "005930", "name": "삼성전자", "upName": "전기·전자"}, {"code": "105560", "name": "KB금융", "upName": ""}]},
        {"return_code": 0, "list": [{"code": "247540", "name": "에코프로비엠", "upName": "일반전기전자"}]},
    ]
    flow_row = lambda code, inst, frgn: {"stk_cd": code, "orgn_cont_netprps_dys": inst, "frgnr_cont_netprps_dys": frgn}  # noqa: E731
    fake.responses["ka10131"] = [
        {"return_code": 0, "orgn_frgnr_cont_trde_prst": [flow_row("005930", "+4", "+2"), flow_row("000660", "+1", "-3")]},
        {"return_code": 0, "orgn_frgnr_cont_trde_prst": [flow_row("247540", "+2", "+2")]},
    ]
    market = KiwoomMarket(client_for(fake), tmp_path)
    listed = {i.symbol: i for i in market.listed_instruments()}
    assert listed["005930"].sector == "전기·전자" and listed["105560"].sector == "기타" and listed["247540"].market == "KOSDAQ"
    assert market.investor_flow() == {"005930": (4, 2), "000660": (1, -3), "247540": (2, 2)}
    market.investor_flow()
    assert sum(1 for api_id, _h, _b in fake.calls if api_id == "ka10131") == 2  # 코스피·코스닥 한 번씩, 30분 캐시
