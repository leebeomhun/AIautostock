from __future__ import annotations

import threading
from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app import ledger
from app.api import views
from app.container import Container
from app.db import now_kst, session_scope
from app.market.universe import UNIVERSES, instrument, register, strategy_symbols, universe_symbols
from app.models import Activity, Strategy
from app.quant.backtest import BacktestParams, run_backtest
from app.quant.signals import TEMPLATES, indicators, template_flag_note
from app.trading.engine import EngineError

router = APIRouter(prefix="/api")

Template = Literal["momentum", "meanrev", "news", "value", "ml_topn"]
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
        "templates": [_template_meta(c, k, v) for k, v in TEMPLATES.items()],
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


def _template_meta(c: Container, key: str, tpl: dict) -> dict:
    note = template_flag_note(key, c.settings)
    if note is None and key == "news" and not c.analyst.enabled:
        note = "OPENAI_API_KEY 가 있어야 쓸 수 있습니다"
    return {"id": key, **tpl, "available": note is None, "note": note}


class BacktestIn(BaseModel):
    # 모르는 키는 거부하지 않고 받아 두었다가 백테스트 결과 warnings 로 알립니다 (backtest_config_warnings).
    model_config = ConfigDict(extra="allow")

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
    # 실시간 엔진과 같은 종목 범위(코스피200 은 시가총액 상위 K200_UNIVERSE_SIZE 까지)와 업종으로 돌립니다.
    try:
        symbols = strategy_symbols(body.universe, body.custom_symbols, c.market, c.settings.k200_universe_size)
        listed = getattr(c.market, "listed_instruments", None)
        if listed is not None:
            register(listed())
    except Exception:  # noqa: BLE001
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
        sectors={sym: instrument(sym).sector for sym in frames},
        max_per_sector=c.settings.max_positions_per_sector,
        price_limit=c.settings.backtest_price_limit,
    )
    result = run_backtest(params, frames, bench)
    if result.ok:
        result.warnings += backtest_config_warnings(body)
    return {**asdict(result), "symbols": len(frames), "data_source": c.market.name}


# 전략 생성 화면이 백테스트에도 같은 본문을 보냅니다. 결과에 영향을 주지만 일봉 백테스트가 반영하지 못하는 설정은 알리고,
# 결과와 무관한 설정(이름·실행 방식)은 조용히 넘깁니다.
BACKTEST_UNMODELED = {"daily_loss_limit": "일일 손실 한도", "exclude_risky": "투자유의 종목 제외"}
BACKTEST_IGNORED = {"name", "exec_mode"}


def backtest_config_warnings(body: BacktestIn) -> list[str]:
    extra = body.model_extra or {}
    out = []
    unknown = sorted(k for k in extra if k not in BACKTEST_UNMODELED and k not in BACKTEST_IGNORED)
    if unknown:
        out.append(f"알 수 없는 설정 키라 무시했습니다: {', '.join(unknown)}")
    unmodeled = [label for key, label in BACKTEST_UNMODELED.items() if extra.get(key)]
    if unmodeled:
        out.append(f"백테스트에 반영하지 않은 설정: {', '.join(unmodeled)} (과거 이력·장중 손익을 일봉으로 알 수 없음)")
    if body.custom_symbols and body.universe != "custom":
        out.append("종목 범위가 '직접 고르기'가 아니어서 custom_symbols 는 쓰지 않았습니다")
    return out


class StrategyIn(BacktestIn):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    exclude_risky: bool = True
    daily_loss_limit: int = Field(default=300_000, ge=0)
    # 비우면 모의투자는 완전 자동, 실거래 계좌는 승인 후 주문으로 정합니다 (create_strategy).
    exec_mode: Mode | None = None


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


def default_exec_mode(c: Container) -> Mode:
    """새 전략의 기본 실행 방식. 실제로 쓰는 모드는 계좌 실행 방식(설정)과 이 값 중 낮은 쪽이고,
    실거래 계좌는 실거래 확인(live_confirmed) 전까지 알림만 보냅니다 (TradingEngine.effective_mode)."""
    return "approve" if c.broker.is_live else "auto"


def create_strategy(c: Container, body: StrategyIn) -> dict:
    if body.universe == "sp500" and c.broker.kind == "kiwoom":
        raise HTTPException(status_code=400, detail="키움 계좌는 국내 주식만 주문합니다")
    if body.template == "news" and not c.analyst.enabled:
        raise HTTPException(status_code=400, detail="AI 뉴스 감성 전략은 OPENAI_API_KEY 가 있어야 합니다")
    flag_note = template_flag_note(body.template, c.settings)
    if flag_note:
        raise HTTPException(status_code=400, detail=f"{TEMPLATES[body.template]['name']} 전략은 {flag_note}")
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
            exec_mode=body.exec_mode or default_exec_mode(c),
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
    status: Literal["running", "paused", "stopped"] | None = None
    # 맡긴 금액(원). 줄여도 보유 종목을 팔지 않고, 평가금액이 새 배분을 넘는 동안 신규 매수만 멈춥니다.
    allocation: int | None = Field(default=None, gt=0)


@router.patch("/strategies/{strategy_id}")
def patch_strategy(request: Request, strategy_id: int, body: StrategyPatch) -> dict:
    c = container(request)
    if body.status is None and body.allocation is None:
        raise HTTPException(status_code=400, detail="바꿀 항목(status 또는 allocation)을 보내 주세요")
    with c.engine.lock, session_scope(c.session_factory) as s:
        st = s.get(Strategy, strategy_id)
        if st is None:
            raise HTTPException(status_code=404, detail="전략을 찾을 수 없습니다")
        if body.allocation is not None and body.allocation != st.allocation:
            others = s.scalar(
                select(func.coalesce(func.sum(Strategy.allocation), 0)).where(Strategy.status != "stopped", Strategy.id != st.id)
            ) or 0
            equity = c.engine.account().equity
            if st.status != "stopped" and others + body.allocation > equity:
                raise HTTPException(
                    status_code=400,
                    detail=f"전략 배분 합계({others + body.allocation:,.0f}원)가 계좌 평가자산({equity:,.0f}원)을 넘습니다. 다른 전략 배분을 먼저 줄여 주세요",
                )
            before, st.allocation = st.allocation, body.allocation
            s.add(Activity(kind="strategy", title=f"{st.name} 배분 변경", detail=f"{before:,}원 → {body.allocation:,}원 · 보유 종목은 그대로 두고 신규 매수 한도만 바꿉니다"))
            ledger.append(s, "strategy.allocation", str(st.id), {"from": before, "to": body.allocation}, now_kst())
        if body.status is not None:
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


def profile_template(c: Container, profile: str) -> str:
    """투자 성향별 추천 전략. 설정으로 꺼 둔 전략(가치 등)이면 평균회귀로 시작합니다."""
    template = PROFILE_TEMPLATE.get(profile, "meanrev")
    return "meanrev" if template_flag_note(template, c.settings) else template


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
                template=profile_template(c, profile),
                universe="k200",
                amount=amount,
                stop_pct=5,
                take_pct=10,
                max_weight_pct=15,
                daily_loss_limit=loss,
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
