from __future__ import annotations

import threading
from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app import ledger
from app.api import views
from app.container import Container
from app.db import now_kst, session_scope
from app.market.universe import UNIVERSES, instrument, universe_symbols
from app.models import Activity, Strategy
from app.quant.backtest import BacktestParams, run_backtest
from app.quant.signals import TEMPLATES, indicators
from app.trading.engine import EngineError

router = APIRouter(prefix="/api")

Template = Literal["momentum", "meanrev", "news", "value"]
Universe = Literal["k200", "kq150", "sp500", "custom"]
Mode = Literal["notify", "approve", "auto"]


def container(request: Request) -> Container:
    return request.app.state.container


def _engine_call(fn, *args):  # noqa: ANN001, ANN202
    try:
        return fn(*args)
    except EngineError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ---------------------------------------------------------------- 조회
@router.get("/dashboard")
def get_dashboard(request: Request) -> dict:
    return views.dashboard(container(request))


@router.get("/performance")
def get_performance(request: Request, period: Literal["1w", "1m", "3m", "1y"] = "1m") -> dict:
    return views.performance(container(request), period)


@router.get("/decisions")
def list_decisions(request: Request, limit: int = 100, status: str | None = None) -> list[dict]:
    return views.decisions_list(container(request), min(limit, 500), status)


@router.get("/decisions/{decision_id}")
def get_decision(request: Request, decision_id: str) -> dict:
    data = views.decision_detail(container(request), decision_id)
    if data is None:
        raise HTTPException(status_code=404, detail="판단을 찾을 수 없습니다")
    return data


@router.get("/report")
def get_report(request: Request, period: Literal["1w", "1m", "3m", "1y"] = "1m") -> dict:
    return views.report(container(request), period)


# ---------------------------------------------------------------- 승인
class ApproveIn(BaseModel):
    qty: int | None = None


class RejectIn(BaseModel):
    feedback: str | None = None


@router.post("/decisions/{decision_id}/approve")
def approve(request: Request, decision_id: str, body: ApproveIn) -> dict:
    c = container(request)
    _engine_call(c.engine.approve, decision_id, body.qty)
    return views.decision_detail(c, decision_id) or {}


@router.post("/decisions/{decision_id}/reject")
def reject(request: Request, decision_id: str, body: RejectIn) -> dict:
    c = container(request)
    _engine_call(c.engine.reject, decision_id, body.feedback)
    return views.decision_detail(c, decision_id) or {}


@router.post("/decisions/{decision_id}/reopen")
def reopen(request: Request, decision_id: str) -> dict:
    c = container(request)
    _engine_call(c.engine.reopen, decision_id)
    return views.decision_detail(c, decision_id) or {}


@router.post("/decisions/{decision_id}/cancel-order")
def cancel_order(request: Request, decision_id: str) -> dict:
    c = container(request)
    message = _engine_call(c.engine.cancel_order, decision_id)
    return {"message": message, "decision": views.decision_detail(c, decision_id)}


# ---------------------------------------------------------------- 제어
class ModeIn(BaseModel):
    mode: Mode


@router.post("/control/mode")
def set_mode(request: Request, body: ModeIn) -> dict:
    c = container(request)
    _engine_call(c.engine.set_mode, body.mode)
    return {"ok": True}


@router.post("/control/stop")
def stop(request: Request) -> dict:
    return {"cancelled": container(request).engine.stop()}


@router.post("/control/start")
def start(request: Request) -> dict:
    container(request).engine.start()
    return {"ok": True}


@router.post("/engine/tick")
def manual_tick(request: Request) -> dict:
    """엔진 한 주기를 바로 실행합니다 (개발·점검용). 백그라운드 주기와 같은 잠금을 씁니다."""
    c = container(request)
    t = threading.Thread(target=c.engine.tick, daemon=True)
    t.start()
    t.join(timeout=120)
    return {"last_tick_at": c.engine.last_tick_at.isoformat() if c.engine.last_tick_at else None, "error": c.engine.last_error}


# ---------------------------------------------------------------- 전략
@router.get("/strategies/meta")
def strategies_meta(request: Request) -> dict:
    c = container(request)
    kiwoom = c.broker.kind == "kiwoom"
    settings = views.settings_view(c)
    universes = []
    for key, u in UNIVERSES.items():
        available = u["domestic"] or not kiwoom
        universes.append(
            {
                "id": key,
                "label": u["label"],
                "available": available,
                "note": None if available else "키움 계좌는 국내 주식만 주문합니다",
                "symbols": [{"symbol": i.symbol, "name": i.name} for i in u["instruments"]],
            }
        )
    pickable = [s for u in universes if u["available"] and u["id"] != "custom" for s in u["symbols"]]
    with session_scope(c.session_factory) as s:
        allocated = s.scalar(select(func.coalesce(func.sum(Strategy.allocation), 0)).where(Strategy.status != "stopped")) or 0
    return {
        **views.meta(c),
        "templates": [{"id": k, **v, "available": not (k == "news" and not c.analyst.enabled)} for k, v in TEMPLATES.items()],
        "universes": universes,
        "pickable_symbols": pickable,
        "cash": settings["cash"],
        "equity": settings["equity"],
        "allocated": int(allocated),
        "paper_days": settings["paper_days"],
        "paper_days_required": settings["paper_days_required"],
        "live_unlocked": settings["live_unlocked"],
        "defaults": {"daily_loss_limit": settings["daily_loss_limit"]},
    }


class BacktestIn(BaseModel):
    template: Template
    universe: Universe = "k200"
    custom_symbols: list[str] = Field(default_factory=list)
    amount: int = Field(gt=0)
    stop_pct: float = Field(ge=1, le=30)
    take_pct: float = Field(ge=1, le=100)
    max_weight_pct: float = Field(ge=1, le=100)
    max_orders_per_day: int = Field(default=20, ge=1, le=200)


@router.post("/backtest")
def backtest(request: Request, body: BacktestIn) -> dict:
    c = container(request)
    symbols = universe_symbols(body.universe, body.custom_symbols)
    if not symbols:
        raise HTTPException(status_code=400, detail="종목을 하나 이상 골라 주세요")
    days = 3 * 252 + 300
    key = (tuple(sorted(symbols)), now_kst().strftime("%Y-%m-%d"))
    cached = c.backtest_cache.get(key)
    if cached is None:
        frames = {}
        for sym in symbols:
            try:
                frames[sym] = indicators(c.market.history(sym, days))
            except Exception:  # noqa: BLE001,S112
                continue
        bench = c.market.benchmark_history(days)
        cached = (frames, bench)
        c.backtest_cache.clear()
        c.backtest_cache[key] = cached
    frames, bench = cached
    params = BacktestParams(
        template=body.template,
        amount=body.amount,
        stop_pct=body.stop_pct,
        take_pct=body.take_pct,
        max_weight_pct=body.max_weight_pct,
        max_orders_per_day=body.max_orders_per_day,
        fee_rate=c.settings.fee_rate,
        sell_tax_rate=c.settings.sell_tax_rate,
        slippage_rate=c.settings.slippage_rate,
    )
    result = run_backtest(params, frames, bench)
    return {**asdict(result), "symbols": len(frames), "data_source": c.market.name}


class StrategyIn(BacktestIn):
    name: str | None = None
    exclude_risky: bool = True
    daily_loss_limit: int = Field(default=300_000, ge=0)
    exec_mode: Mode = "approve"


def _strategy_dict(st: Strategy) -> dict:
    return {
        "id": st.id,
        "name": st.name,
        "template": st.template,
        "version": st.version,
        "universe": st.universe,
        "allocation": st.allocation,
        "stop_pct": st.stop_pct,
        "take_pct": st.take_pct,
        "max_weight_pct": st.max_weight_pct,
        "daily_loss_limit": st.daily_loss_limit,
        "max_orders_per_day": st.max_orders_per_day,
        "exec_mode": st.exec_mode,
        "status": st.status,
        "pause_reason": st.pause_reason,
    }


@router.get("/strategies")
def list_strategies(request: Request) -> list[dict]:
    with session_scope(container(request).session_factory) as s:
        return [_strategy_dict(st) for st in s.scalars(select(Strategy).order_by(Strategy.id)).all()]


def create_strategy(c: Container, body: StrategyIn) -> dict:
    if body.universe == "sp500" and c.broker.kind == "kiwoom":
        raise HTTPException(status_code=400, detail="키움 계좌는 국내 주식만 주문합니다")
    if body.template == "news" and not c.analyst.enabled:
        raise HTTPException(status_code=400, detail="AI 뉴스 감성 전략은 OPENAI_API_KEY 가 있어야 합니다")
    symbols = universe_symbols(body.universe, body.custom_symbols)
    if not symbols:
        raise HTTPException(status_code=400, detail="종목을 하나 이상 골라 주세요")
    acct = c.engine.account()
    if body.amount > acct.equity:
        raise HTTPException(status_code=400, detail="맡길 금액이 계좌 평가자산보다 큽니다")
    tpl = TEMPLATES[body.template]
    with session_scope(c.session_factory) as s:
        st = Strategy(
            name=(body.name or tpl["name"]).strip()[:60],
            template=body.template,
            version=tpl["version"],
            universe=body.universe,
            custom_symbols=[x for x in body.custom_symbols if x] if body.universe == "custom" else [],
            exclude_risky=body.exclude_risky,
            allocation=body.amount,
            stop_pct=body.stop_pct,
            take_pct=body.take_pct,
            max_weight_pct=body.max_weight_pct,
            daily_loss_limit=body.daily_loss_limit,
            max_orders_per_day=body.max_orders_per_day,
            exec_mode=body.exec_mode,
            status="running",
        )
        s.add(st)
        s.flush()
        names = ", ".join(instrument(x).name for x in symbols[:3]) + (f" 외 {len(symbols) - 3}종목" if len(symbols) > 3 else "")
        s.add(Activity(kind="strategy", title=f"{st.name} 전략 시작", detail=f"{names} · 맡긴 금액 {body.amount:,}원"))
        return _strategy_dict(st)


@router.post("/strategies")
def post_strategy(request: Request, body: StrategyIn) -> dict:
    return create_strategy(container(request), body)


class StrategyPatch(BaseModel):
    status: Literal["running", "paused", "stopped"]


@router.patch("/strategies/{strategy_id}")
def patch_strategy(request: Request, strategy_id: int, body: StrategyPatch) -> dict:
    c = container(request)
    with c.engine.lock, session_scope(c.session_factory) as s:
        st = s.get(Strategy, strategy_id)
        if st is None:
            raise HTTPException(status_code=404, detail="전략을 찾을 수 없습니다")
        st.status, st.paused_auto = body.status, False
        st.pause_reason = "사용자가 일시정지 · 보유 종목 손절·익절은 계속 관리합니다" if body.status == "paused" else None
        label = {"running": "다시 실행", "paused": "일시정지", "stopped": "중지"}[body.status]
        s.add(Activity(kind="strategy", title=f"{st.name} 전략 {label}", detail=st.pause_reason or ""))
        return _strategy_dict(st)


# ---------------------------------------------------------------- 설정 · 온보딩
@router.get("/settings")
def get_settings(request: Request) -> dict:
    return views.settings_view(container(request))


class SettingsIn(BaseModel):
    risk_profile: Literal["conservative", "neutral", "aggressive"] | None = None
    invest_amount: int | None = Field(default=None, gt=0)
    daily_loss_limit: int | None = Field(default=None, gt=0)
    limit_action: Literal["buy", "all"] | None = None
    agreed: bool | None = None


@router.put("/settings")
def put_settings(request: Request, body: SettingsIn) -> dict:
    c = container(request)
    with c.engine.lock, session_scope(c.session_factory) as s:
        st = c.engine.account_settings(s)
        for k, v in body.model_dump(exclude_none=True).items():
            setattr(st, k, v)
    return views.settings_view(c)


PROFILE_TEMPLATE = {"conservative": "value", "neutral": "meanrev", "aggressive": "momentum"}


@router.post("/onboarding/complete")
def complete_onboarding(request: Request) -> dict:
    c = container(request)
    with session_scope(c.session_factory) as s:
        st = c.engine.account_settings(s)
        if not st.agreed:
            raise HTTPException(status_code=400, detail="투자 위험 안내에 동의해 주세요")
        profile, amount, loss = st.risk_profile, st.invest_amount, st.daily_loss_limit
        has_strategy = bool(s.scalar(select(func.count()).select_from(Strategy)))
    if not has_strategy:
        acct = c.engine.account()
        amount = min(amount, int(acct.equity))
        create_strategy(
            c,
            StrategyIn(
                template=PROFILE_TEMPLATE.get(profile, "meanrev"),
                universe="k200",
                amount=amount,
                stop_pct=5,
                take_pct=10,
                max_weight_pct=15,
                daily_loss_limit=loss,
                exec_mode="approve",
            ),
        )
    with session_scope(c.session_factory) as s:
        st = c.engine.account_settings(s)
        st.onboarded = True
        st.paper_started_at = st.paper_started_at or now_kst()
        s.add(Activity(kind="system", title="모의투자 시작", detail=f"일일 손실 한도 −{st.daily_loss_limit:,}원 · 승인 후 주문 모드"))
    return views.settings_view(c)


@router.post("/settings/live-confirm")
def live_confirm(request: Request) -> dict:
    c = container(request)
    if not c.broker.is_live:
        raise HTTPException(status_code=400, detail="실전 계좌가 연결되어 있지 않습니다 (KIWOOM_MODE=real)")
    view = views.settings_view(c)
    if not view["live_unlocked"]:
        raise HTTPException(status_code=400, detail=f"모의투자 {view['paper_days_required']}일 운영 후 열립니다")
    with c.engine.lock, session_scope(c.session_factory) as s:
        st = c.engine.account_settings(s)
        st.live_confirmed = True
        s.add(Activity(kind="system", title="실전 주문 허용", detail="이제 승인 후 주문·완전 자동 모드에서 실전 계좌로 주문합니다"))
        ledger.append(s, "control.live_confirmed", None, {"broker": c.broker.label}, now_kst())
    return views.settings_view(c)


# ---------------------------------------------------------------- 원장 · 배치 결과
@router.get("/ledger/verify")
def verify_ledger(request: Request) -> dict:
    with session_scope(container(request).session_factory) as s:
        return ledger.verify(s)


@router.get("/insights/research/{symbol}")
def research_report(request: Request, symbol: str) -> dict:
    data = container(request).insights.research_report(symbol)
    if data is None:
        raise HTTPException(status_code=404, detail="이 종목의 심층 리서치가 없습니다")
    return data
