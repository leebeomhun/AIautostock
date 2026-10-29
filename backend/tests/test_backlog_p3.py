"""백로그 P3: 플래그 뒤 전략 — ML 상위 주간 리밸런싱(스캐폴드)과 가치 리밸런싱. 둘 다 기본 꺼짐."""

import json
from datetime import timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.routes import StrategyIn, create_strategy
from app.db import now_kst, session_scope
from app.main import create_app
from app.models import Activity, Decision, Position, Strategy
from app.quant.backtest import BacktestParams, run_backtest


def _body(template: str, symbols: list[str] | None = None) -> StrategyIn:
    universe = "custom" if symbols else "k200"
    return StrategyIn(template=template, universe=universe, custom_symbols=symbols or [], amount=5_000_000, stop_pct=5, take_pct=10, max_weight_pct=15)


def _buys(c):
    return {p.candidate.symbol: p for p in c.engine._prepare(now_kst()) if p.candidate.side == "buy"}  # noqa: SLF001


def _write_ml(c, pcts: dict[str, float], fold_ics: list[float], null_t: float = 3.0) -> None:
    (c.settings.data_dir / "ml").mkdir(parents=True, exist_ok=True)
    ranked = sorted(pcts, key=pcts.get)
    scores = {s: {"score": 1 - pcts[s], "rank": ranked.index(s) + 1, "pct": pcts[s]} for s in pcts}
    metrics = {"valid_rank_ic": sum(fold_ics) / len(fold_ics), "fold_rank_ics": fold_ics, "label_horizon": 5, "random_null": {"alpha_t": null_t}}
    (c.settings.data_dir / "ml" / "latest.json").write_text(json.dumps({"as_of": now_kst().strftime("%Y-%m-%d"), "model": "m", "metrics": metrics, "scores": scores}))


def test_flagged_strategies_are_off_by_default(settings, container):
    c = container
    assert not c.settings.enable_ml_topn_strategy and not c.settings.enable_value_strategy
    with TestClient(create_app(settings, c)) as http:
        meta = {t["id"]: t for t in http.get("/api/strategies/meta").json()["templates"]}
        assert not meta["ml_topn"]["available"] and "백테스트할 수 없습니다" in meta["ml_topn"]["note"]
        assert not meta["value"]["available"] and "PER·PBR 과거 이력" in meta["value"]["note"]
        assert meta["momentum"]["available"] and meta["momentum"]["note"] is None
        bt = http.post("/api/backtest", json={"template": "ml_topn", "universe": "k200", "amount": 5_000_000, "stop_pct": 5, "take_pct": 10, "max_weight_pct": 15}).json()
        assert bt["ok"] is False and "ML 예측" in bt["message"]
        # 보수형 성향 추천(가치)이 꺼져 있으면 평균회귀로 시작합니다.
        http.put("/api/settings", json={"risk_profile": "conservative", "invest_amount": 5_000_000, "daily_loss_limit": 100_000, "limit_action": "buy", "agreed": True})
        http.post("/api/onboarding/complete")
        assert [s["template"] for s in http.get("/api/strategies").json()] == ["meanrev"]
    for template in ("ml_topn", "value"):
        with pytest.raises(HTTPException) as exc:
            create_strategy(c, _body(template))
        assert exc.value.status_code == 400
    assert not run_backtest(BacktestParams(template="ml_topn", amount=1, stop_pct=5, take_pct=10, max_weight_pct=15), {}, None).ok


def test_existing_value_strategy_stops_buying_when_flag_is_off(container):
    c = container
    c.settings.enable_value_strategy = True
    create_strategy(c, _body("value"))
    assert _buys(c)
    c.settings.enable_value_strategy = False
    assert _buys(c) == {}


def test_ml_topn_rebalances_weekly_to_validated_top_ranks(container):
    c = container
    c.settings.enable_ml_topn_strategy = True
    c.settings.ml_topn_size = 2
    syms = ["005930", "000660", "005380", "000270"]
    _write_ml(c, {"005930": 1.0, "000660": 0.0, "005380": 0.66, "000270": 0.33}, [0.05, 0.04, 0.03])
    sid = create_strategy(c, _body("ml_topn", syms))["id"]
    with session_scope(c.session_factory) as s:
        s.add(Position(strategy_id=sid, symbol="005930", name="삼성전자", qty=3, avg_price=c.market.quote("005930").price, opened_at=now_kst() - timedelta(days=9)))
    c.engine.tick()
    week = f"{now_kst().isocalendar().year}-W{now_kst().isocalendar().week:02d}"
    with session_scope(c.session_factory) as s:
        decided = {(d.symbol, d.side, d.reason) for d in s.scalars(select(Decision).where(Decision.strategy_id == sid)).all()}
        assert s.get(Strategy, sid).last_rebalanced == week
    assert ("000660", "buy", "ml_rebalance_buy") in decided and ("000270", "buy", "ml_rebalance_buy") in decided
    assert ("005930", "sell", "ml_rebalance") in decided  # 상위 2종목에서 빠진 보유 종목
    assert not any(sym == "005380" for sym, _side, _r in decided)
    # 같은 주에는 다시 리밸런싱하지 않습니다.
    assert not any(p.candidate.reason == "ml_rebalance_buy" for p in c.engine._prepare(now_kst()))  # noqa: SLF001


def test_ml_topn_holds_off_when_ml_is_not_validated(container):
    c = container
    c.settings.enable_ml_topn_strategy = True
    _write_ml(c, {"005930": 0.0, "000660": 1.0}, [0.12, -0.01, -0.02])
    sid = create_strategy(c, _body("ml_topn", ["005930", "000660"]))["id"]
    assert _buys(c) == {}
    with session_scope(c.session_factory) as s:
        assert s.get(Strategy, sid).last_rebalanced is None
        held = s.scalar(select(Activity).where(Activity.title.like("%리밸런싱 보류")))
        assert held is not None and "검증 미달" in held.detail
