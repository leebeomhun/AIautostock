import json
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import select

from app.api import views
from app.db import now_kst, session_scope
from app.insights import Insights
from app.kiwoom.market import KiwoomMarket
from app.models import Decision, Position
from app.quant import signals
from app.api.routes import StrategyIn, create_strategy
from app.quant.signals import (
    Candidate,
    Signal,
    entry_frame,
    indicators,
    project_partial_bar,
    risk_levels,
    session_volume_fraction,
    signal_history,
    template_exit,
)
from tests.test_engine import add_decision, add_strategy
from tests.test_kiwoom import FakeKiwoom, client_for


def _row(close, ma20):
    return pd.Series({"close": close, "ma20": ma20})


def test_trend_break_fires_every_day_below_ma20_not_only_on_cross():
    # 어제도 이미 20일선 아래였어도(교차한 날이 지나도) 청산 신호가 납니다.
    assert template_exit("momentum", _row(95, 100), hold_days=3, pnl_pct=-0.01) == "trend_break"
    assert template_exit("momentum", _row(105, 100), hold_days=3, pnl_pct=0.02) is None


def test_time_exit_only_cuts_stalled_momentum_and_skips_meanrev():
    assert template_exit("momentum", _row(105, 100), hold_days=10, pnl_pct=0.01) == "time_exit"
    assert template_exit("momentum", _row(130, 100), hold_days=10, pnl_pct=0.25) is None
    assert template_exit("meanrev", _row(95, 100), hold_days=30, pnl_pct=-0.02) is None
    assert template_exit("news", _row(95, 100), hold_days=3, pnl_pct=0.05) == "time_exit"


def test_risk_levels_widen_stop_and_shrink_size_for_volatile_meanrev():
    assert risk_levels("momentum", 5, 10, 60.0) == signals.RiskLevels(5, 10, 1.0)
    calm = risk_levels("meanrev", 5, 10, 20.0)
    assert calm.stop_pct == 5 and calm.size_scale == 1.0 and not calm.widened
    wild = risk_levels("meanrev", 5, 10, 60.0)
    assert 9 < wild.stop_pct < 10 and abs(wild.stop_pct * wild.size_scale - 5) < 0.01 and wild.take_pct > 18
    capped = risk_levels("meanrev", 5, 10, 120.0)
    assert capped.stop_pct == 10 and capped.size_scale == 0.5
    assert risk_levels("meanrev", 5, 10, None).size_scale == 1.0


def test_expired_stop_loss_is_proposed_again(container):
    c = container
    sid = add_strategy(c, template="meanrev", exec_mode="approve")
    price = c.market.quote("005930").price
    with session_scope(c.session_factory) as s:
        s.add(Position(strategy_id=sid, symbol="005930", name="삼성전자", qty=10, avg_price=price * 1.3, opened_at=now_kst() - timedelta(days=5)))
    c.engine.tick()
    with session_scope(c.session_factory) as s:
        first = s.scalar(select(Decision).where(Decision.reason == "stop_loss", Decision.status == "pending"))
        assert first is not None
        first.expires_at = now_kst() - timedelta(seconds=1)
        first_id = first.id
    c.engine.tick()
    with session_scope(c.session_factory) as s:
        assert s.get(Decision, first_id).status == "expired"
        again = s.scalars(select(Decision).where(Decision.reason == "stop_loss", Decision.status == "pending")).all()
        assert len(again) == 1 and again[0].id != first_id


def test_close_window_gates_momentum_entries(container, monkeypatch):
    c = container
    add_strategy(c, template="momentum", exec_mode="approve")
    c.market.always_open = False
    c.settings.close_window = "15:00-15:20"
    assert not c.engine.in_close_window(datetime(2026, 10, 28, 14, 59))
    assert c.engine.in_close_window(datetime(2026, 10, 28, 15, 0))
    assert not c.engine.in_close_window(datetime(2026, 10, 28, 15, 20))

    def always_breakout(template, symbol, name, ind, bench):
        return Candidate(symbol, name, "buy", "breakout", 0.6, [Signal("breakout", "돌파", "테스트", "buy", 0.6)])

    monkeypatch.setattr("app.trading.engine.describe_entry", always_breakout)
    assert [p for p in c.engine._prepare(datetime(2026, 10, 28, 10, 0)) if p.candidate.side == "buy"] == []  # noqa: SLF001
    assert [p for p in c.engine._prepare(datetime(2026, 10, 28, 15, 5)) if p.candidate.side == "buy"]  # noqa: SLF001
    c.settings.close_window = ""
    assert c.engine.in_close_window(datetime(2026, 10, 28, 10, 0))


def test_strategies_do_not_buy_the_same_symbol_in_one_tick(container):
    c = container
    add_strategy(c, template="value", exec_mode="approve")
    add_strategy(c, template="value", exec_mode="approve")
    c.engine.tick()
    with session_scope(c.session_factory) as s:
        buys = [d.symbol for d in s.scalars(select(Decision).where(Decision.side == "buy", Decision.status == "pending")).all()]
    assert buys and len(buys) == len(set(buys))


def test_signal_history_reports_average_return_after_costs(container):
    frames = {sym: indicators(container.market.history(sym, 400)) for sym in ("005930", "000660", "005380", "000270", "105560", "035420")}
    # 매도 신호의 평균은 '계속 보유했을 때'의 등락이라 비용을 빼지 않습니다.
    sell = signal_history("trend_break", frames, 5, 10)
    assert sell is not None and signal_history("trend_break", frames, 5, 10, round_trip_cost=0.01)["avg_return"] == sell["avg_return"]
    for reason in ("breakout", "meanrev_entry"):
        gross = signal_history(reason, frames, 5, 10)
        if gross:
            net = signal_history(reason, frames, 5, 10, round_trip_cost=0.01)
            assert abs(net["avg_return"] - (gross["avg_return"] - 1.0)) < 0.011


def test_ai_review_compares_followed_and_held_decisions(container):
    c = container
    sid = add_strategy(c)
    close = c.market.history("005930", 250)["close"]
    when = close.index[-15]
    ids = [add_decision(c, sid, symbol="005930"), add_decision(c, sid, symbol="005930"), add_decision(c, sid, symbol="005930", side="sell", reason="stop_loss")]
    with session_scope(c.session_factory) as s:
        for did, status in zip(ids, ("notified", "held", "notified"), strict=True):
            d = s.get(Decision, did)
            d.status, d.model, d.price, d.created_at = status, "gpt-test", float(close.loc[when]), when.to_pydatetime() + timedelta(hours=15)
    review = views.report(c, "1m")["ai_review"]
    expected = round((float(close.iloc[-10]) / float(close.loc[when]) - 1) * 100, 2)
    assert review["buy"]["followed"] == {"count": 1, "avg_return": expected}
    assert review["buy"]["held"]["count"] == 1 and review["buy"]["edge"] == 0.0
    assert review["sell"]["followed"]["count"] == 0  # 손절은 AI 가 보류할 수 없어 뺍니다


def test_ml_signal_ignored_when_most_folds_are_negative(tmp_path):
    today = now_kst().strftime("%Y-%m-%d")
    (tmp_path / "ml").mkdir()
    metrics = {"valid_rank_ic": 0.05, "fold_rank_ics": [0.17, -0.01, -0.01], "label_horizon": 5}
    (tmp_path / "ml" / "latest.json").write_text(json.dumps({"as_of": today, "model": "m", "metrics": metrics, "scores": {"005930": {"score": 1, "rank": 1, "pct": 0.0}}}))
    sig, adjust = Insights(tmp_path).ml_signal("005930", "buy", now_kst().date())
    assert sig.side == "neutral" and adjust == 0.0 and "1개만 양수" in sig.detail and "향후 5거래일" in sig.detail


def test_top_by_market_cap_keeps_common_stocks_only(tmp_path):
    fake = FakeKiwoom()

    def row(code, name, market, count, price, state=""):
        return {"code": code, "name": name, "marketName": market, "listCount": f"{count:016d}", "lastPrice": f"{price:08d}", "state": state}

    fake.responses["ka10099"] = [{"return_code": 0, "list": [
        row("005930", "삼성전자", "거래소", 5_000_000_000, 70_000),
        row("005935", "삼성전자우", "거래소", 800_000_000, 60_000),
        row("069500", "KODEX 200", "ETF", 900_000_000, 40_000),
        row("123450", "가나스팩1호", "거래소", 9_000_000_000, 2_000),
        row("111110", "관리기업", "거래소", 9_000_000_000, 2_000, state="관리종목"),
        row("000660", "SK하이닉스", "거래소", 700_000_000, 200_000),
        row("222220", "작은회사", "거래소", 10_000_000, 1_000),
    ]}]
    market = KiwoomMarket(client_for(fake), tmp_path)
    assert market.top_by_market_cap(2) == ["005930", "000660"]
    assert market.top_by_market_cap(10) == ["005930", "000660", "222220"]
    assert sum(1 for api_id, _h, _b in fake.calls if api_id == "ka10099") == 1  # 하루 한 번만 조회


def test_bearish_research_shrinks_buy_instead_of_blocking(container, tmp_path, monkeypatch):
    c = container
    today = now_kst().strftime("%Y-%m-%d")
    (tmp_path / "research").mkdir()
    items = {"000660": {"rating": "Underweight", "as_of": today}, "005380": {"rating": "Sell", "as_of": today}}
    (tmp_path / "research" / "latest.json").write_text(json.dumps({"as_of": today, "items": items}))
    c.engine.insights = Insights(tmp_path)
    syms = ["005930", "000660", "005380"]
    create_strategy(c, StrategyIn(template="momentum", universe="custom", custom_symbols=syms, amount=10_000_000, stop_pct=5, take_pct=10, max_weight_pct=15))

    def always_breakout(template, symbol, name, ind, bench):
        return Candidate(symbol, name, "buy", "breakout", 0.6, [Signal("breakout", "돌파", "테스트", "buy", 0.6)])

    monkeypatch.setattr("app.trading.engine.describe_entry", always_breakout)
    buys = {p.candidate.symbol: p for p in c.engine._prepare(now_kst()) if p.candidate.side == "buy"}  # noqa: SLF001
    assert set(buys) == set(syms)  # 약세 의견이어도 막지 않습니다
    # 점수: 중립 0.6 > Underweight 0.5 > Sell 0.4 — 같은 주기에 진입 수가 모자라면 약세 종목이 뒤로 밀립니다.
    assert [round(buys[s].candidate.score, 2) for s in syms] == [0.6, 0.5, 0.4]
    cap = 10_000_000 * 0.15
    spent = {s: buys[s].qty * buys[s].price for s in syms}
    assert spent["005930"] > cap * 0.5
    assert 0 < spent["000660"] <= cap * 0.5 and 0 < spent["005380"] <= cap * 0.25
    assert spent["005380"] < spent["000660"] or buys["005380"].qty == 1
    assert any("Underweight" in n and "50%" in n for n in buys["000660"].risk_notes)
    assert any("Sell" in n and "25%" in n for n in buys["005380"].risk_notes)
    assert not any("리서치" in n for n in buys["005930"].risk_notes)
    assert any(x.key == "research" and x.side == "sell" for x in buys["005380"].candidate.signals)


def test_new_strategy_defaults_to_auto_on_paper_and_approve_on_live(container, monkeypatch):
    c = container
    body = StrategyIn(template="meanrev", universe="k200", amount=1_000_000, stop_pct=5, take_pct=10, max_weight_pct=15)
    assert create_strategy(c, body)["exec_mode"] == "auto"
    monkeypatch.setattr(c.broker, "is_live", True)
    assert create_strategy(c, body)["exec_mode"] == "approve"
    assert create_strategy(c, body.model_copy(update={"exec_mode": "notify"}))["exec_mode"] == "notify"


def test_session_volume_fraction_is_u_shaped_and_one_outside_session():
    at = lambda h, m: session_volume_fraction(datetime(2026, 10, 29, h, m))  # noqa: E731
    assert at(8, 30) == 1.0 and at(15, 30) == 1.0 and at(18, 40) == 1.0
    assert at(9, 1) == signals.MIN_VOLUME_FRACTION  # 장 시작 직후 몇 분치를 크게 부풀리지 않습니다
    assert abs(at(10, 0) - 0.27) < 1e-9 and abs(at(15, 0) - 0.86) < 1e-9
    # 오전 1시간(전체 시간의 15%)에 하루 거래량의 1/4 이상이 몰립니다.
    assert at(10, 0) > 60 / 390
    steps = [at(h, m) for h in range(9, 16) for m in (0, 15, 30, 45) if (h, m) < (15, 30)][1:]
    assert steps == sorted(steps)


def _breakout_today(today_volume: float, day: str = "2026-10-29") -> pd.DataFrame:
    idx = pd.bdate_range(end=day, periods=80)
    close = [90 + i * 10 / 78 for i in range(79)] + [110.0]
    volume = [1000.0] * 79 + [today_volume]
    return indicators(pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": volume}, index=idx))


def test_partial_bar_volume_is_projected_to_a_full_day():
    # 오전 10시까지 20일 평균의 60%가 거래됐으면 하루치로는 약 2.2배 — 그대로 비교하면 1.5배 조건을 못 넘습니다.
    morning = datetime(2026, 10, 29, 10, 0)
    raw = _breakout_today(600)
    assert not entry_frame("momentum", raw)["entry"].iloc[-1]
    projected = project_partial_bar(raw, morning)
    assert abs(projected["vol_ratio"].iloc[-1] - 600 / 0.27 / 1000) < 1e-6
    assert entry_frame("momentum", projected)["entry"].iloc[-1]
    assert projected["vol_fraction"].iloc[-1] == 0.27 and (projected["vol_fraction"].iloc[:-1] == 1.0).all()
    assert raw["vol_ratio"].iloc[-1] == 0.6  # 원본은 바꾸지 않습니다
    cand = signals.describe_entry("momentum", "005930", "삼성전자", projected, None)
    assert cand and "하루치로 환산" in next(s.detail for s in cand.signals if s.key == "volume")


def test_close_window_volume_still_needs_projection():
    # 15:00–15:20 판단 구간에도 하루 거래량의 10–15%는 아직 안 쌓였습니다. 누적 1.4배면 하루치로는 1.5배를 넘습니다.
    raw = _breakout_today(1400)
    assert not entry_frame("momentum", raw)["entry"].iloc[-1]
    assert entry_frame("momentum", project_partial_bar(raw, datetime(2026, 10, 29, 15, 10)))["entry"].iloc[-1]
    # 오늘 봉이 아니거나(장 마감 뒤·전일 봉) 장 밖이면 환산하지 않습니다.
    assert project_partial_bar(raw, datetime(2026, 10, 30, 10, 0)) is raw
    assert project_partial_bar(raw, datetime(2026, 10, 29, 16, 0)) is raw


def _chart_rows(volume: int, close: int = 110) -> dict:
    rows = [{"dt": "20261029", "open_pric": "100", "high_pric": str(close), "low_pric": "99", "cur_prc": str(close), "trde_qty": str(volume)}]
    rows += [{"dt": f"202610{d:02d}", "open_pric": "100", "high_pric": "101", "low_pric": "99", "cur_prc": "100", "trde_qty": "1000"} for d in (28, 27, 26)]
    return {"stk_dt_pole_chart_qry": rows, "return_code": 0}


def test_daily_bar_cache_refreshes_today_during_session(tmp_path, monkeypatch):
    fake = FakeKiwoom()
    fake.responses["ka10081"] = [_chart_rows(100), _chart_rows(500, 112), _chart_rows(900, 115), _chart_rows(950, 115)]
    clock = {"now": datetime(2026, 10, 29, 9, 30)}
    monkeypatch.setattr("app.kiwoom.market.now_kst", lambda: clock["now"])
    market = KiwoomMarket(client_for(fake), tmp_path, intraday_ttl=300)
    chart_calls = lambda: sum(1 for api_id, _h, _b in fake.calls if api_id == "ka10081")  # noqa: E731

    assert market.history("005930", 3)["volume"].iloc[-1] == 100
    clock["now"] = datetime(2026, 10, 29, 9, 33)
    assert market.history("005930", 3)["volume"].iloc[-1] == 100 and chart_calls() == 1  # TTL 안에서는 캐시
    clock["now"] = datetime(2026, 10, 29, 9, 36)
    df = market.history("005930", 3)
    assert chart_calls() == 2 and df["volume"].iloc[-1] == 500 and df["close"].iloc[-1] == 112
    assert list(df.index.strftime("%m-%d")) == ["10-27", "10-28", "10-29"]  # 지난 봉은 그대로 이어 붙입니다

    # 장 마감 뒤에는 장중에 받은 캐시를 한 번만 다시 받아 확정 봉으로 바꾸고, 그다음은 캐시를 씁니다.
    clock["now"] = datetime(2026, 10, 29, 15, 45)
    assert market.history("005930", 3)["volume"].iloc[-1] == 900 and chart_calls() == 3
    clock["now"] = datetime(2026, 10, 29, 18, 40)
    assert market.history("005930", 3)["volume"].iloc[-1] == 900 and chart_calls() == 3

    # 프로세스를 다시 띄워도 파일 캐시의 받은 시각으로 같은 판단을 합니다.
    fresh = KiwoomMarket(client_for(fake), tmp_path, intraday_ttl=300)
    assert fresh.history("005930", 3)["volume"].iloc[-1] == 900 and chart_calls() == 3


def test_old_cache_without_fetch_time_is_refreshed_in_session(tmp_path, monkeypatch):
    (tmp_path / "005930.json").write_text(json.dumps({"as_of": "2026-10-29", "requested": 3, "dates": ["2026-10-28", "2026-10-29"], "open": [100, 100], "high": [101, 101], "low": [99, 99], "close": [100, 100], "volume": [1000, 50]}))
    fake = FakeKiwoom()
    fake.responses["ka10081"] = [_chart_rows(700)]
    monkeypatch.setattr("app.kiwoom.market.now_kst", lambda: datetime(2026, 10, 29, 14, 0))
    assert KiwoomMarket(client_for(fake), tmp_path).history("005930", 3)["volume"].iloc[-1] == 700


def test_newer_quote_overrides_today_bar_but_older_one_does_not(tmp_path, monkeypatch):
    fake = FakeKiwoom()
    fake.responses["ka10081"] = [_chart_rows(500, 112), _chart_rows(800, 113)]
    fake.responses["ka10001"] = [{"cur_prc": "+118", "base_pric": "100", "trde_qty": "650", "high_pric": "+119", "low_pric": "98", "open_pric": "100", "return_code": 0}]
    clock = {"now": datetime(2026, 10, 29, 10, 0)}
    monkeypatch.setattr("app.kiwoom.market.now_kst", lambda: clock["now"])
    market = KiwoomMarket(client_for(fake), tmp_path, intraday_ttl=300)
    market.history("005930", 3)
    clock["now"] = datetime(2026, 10, 29, 10, 1)
    market.quote("005930")
    clock["now"] = datetime(2026, 10, 29, 10, 2)
    bar = market.history("005930", 3).iloc[-1]
    assert (bar["close"], bar["high"], bar["low"], bar["volume"]) == (118, 119, 98, 650)
    # 일봉을 다시 받은 뒤에는 그보다 오래된 현재가로 덮지 않습니다.
    clock["now"] = datetime(2026, 10, 29, 10, 6)
    bar = market.history("005930", 3).iloc[-1]
    assert (bar["close"], bar["volume"]) == (113, 800)
