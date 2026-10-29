"""매매 엔진.

한 주기(tick)는 세 단계로 나뉩니다.
1) 준비 (잠금): 만료·체결 동기화·손실 한도 점검 후, 전략별로 퀀트 후보를 만들고 리스크 가드로 수량을 정합니다.
2) 판단 (잠금 해제): OpenAI 로 뉴스 감성·최종 판단을 받습니다. 이 동안에도 승인/거절/긴급 정지가 막히지 않습니다.
3) 반영 (잠금): 상태를 다시 확인한 뒤 모드(알림만/승인 후 주문/완전 자동)에 맞게 판단을 기록하거나 주문합니다.
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app import ledger
from app.ai.analyst import AIAnalyst, AIDecision, NewsResult
from app.config import Settings
from app.insights import Insights
from app.db import now_kst, session_scope
from app.market.base import MarketData
from app.market.flow_store import save_snapshot
from app.market.universe import instrument, register, strategy_symbols, universe_symbols
from app.models import Activity, AccountSettings, DayState, Decision, EquitySnapshot, Order, Position, Strategy
from app.quant.allocation import regime_allocations
from app.quant.signals import (
    CLOSE_WINDOW_TEMPLATES,
    NEWS_VOLATILITY_PAUSE,
    REBALANCE_TEMPLATES,
    TEMPLATES,
    WEAK_MARKET_MA,
    WEAK_MARKET_SIZE,
    Candidate,
    RiskLevels,
    Signal,
    clamp,
    common_exit,
    describe_entry,
    describe_exit,
    flow_signal,
    indicators,
    pct,
    peak_close,
    project_partial_bar,
    risk_levels,
    sector_full,
    signal_history,
    template_flag_note,
    template_exit,
    trailing_exit,
    trailing_stop_pct,
    value_ranking,
    vol_before,
    weak_market,
)
from app.trading.broker import Broker, BrokerAccount
from app.trading.protections import ProtectionConfig, account_block, symbol_block
from app.trading.risk import LossState, check_buy, check_sell, size_buy

log = logging.getLogger(__name__)

MODE_RANK = {"notify": 0, "approve": 1, "auto": 2}
MODE_LABEL = {"notify": "알림만", "approve": "승인 후 주문", "auto": "완전 자동"}
OPEN_ORDER_STATES = ("submitted", "partial")
MAX_ENTRIES_PER_TICK = 3
FRAME_DAYS = 400


class EngineError(RuntimeError):
    pass


@dataclass
class TickBudget:
    """한 주기 안에서 전략들이 함께 쓰는 현금과 종목. 전략마다 같은 현금을 기준으로 수량을 정하지 않게 합니다."""

    cash: float
    claimed: set[str]  # 이미 보유 중이거나 이번 주기에 매수 후보로 잡힌 종목


@dataclass
class Prepared:
    strategy_id: int
    candidate: Candidate
    qty: int
    price: float
    avg_price: float | None
    context: dict[str, Any]
    chart: dict[str, Any]
    history_stat: dict[str, Any] | None
    risk_rules: list[str]
    risk_notes: list[str]
    stop_pct: float
    take_pct: float
    needs_news: bool = False
    news: NewsResult | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def strategy_label(strat: Strategy) -> str:
    return f"{strat.name} {strat.version}"


def risk_rule_texts(strat: Strategy, levels: RiskLevels, trail_pct: float | None = None) -> list[str]:
    rules = [f"손절 −{levels.stop_pct:g}%", f"익절 +{levels.take_pct:g}%", f"종목 비중 {strat.max_weight_pct * levels.size_scale:.0f}%"]
    if trail_pct:
        rules.append(f"일부 익절 뒤 최고 종가 대비 −{trail_pct:g}% 트레일링")
    if levels.widened:
        rules.append(f"변동성 반영 · 설정 손절 −{strat.stop_pct:g}%·비중 {strat.max_weight_pct:g}% 기준")
    return rules


def fmt_won(x: float, sign: bool = False) -> str:
    v = round(x)
    s = f"{abs(v):,}"
    if sign:
        return ("+" if v > 0 else "−" if v < 0 else "") + s
    return ("−" if v < 0 else "") + s


class TradingEngine:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        market: MarketData,
        broker: Broker,
        analyst: AIAnalyst,
        insights: Insights | None = None,
    ) -> None:
        self.settings = settings
        self.insights = insights
        self.protections = ProtectionConfig(
            stoploss_cooldown_days=settings.protect_stoploss_cooldown_days,
            exit_cooldown_days=settings.protect_exit_cooldown_days,
            stoploss_guard_count=settings.protect_stoploss_guard_count,
            max_drawdown_pct=settings.protect_max_drawdown_pct,
        )
        self.session_factory = session_factory
        self.market = market
        self.broker = broker
        self.analyst = analyst
        self.lock = threading.RLock()
        self.last_tick_at: datetime | None = None
        self.last_error: str | None = None
        self._last_snapshot: datetime | None = None
        self._stat_cache: dict[tuple, tuple[Any, dict[str, Any] | None]] = {}
        self._listed_on: Any = None
        self._flow_saved_on: Any = None
        self._news_cache: dict[tuple[str, str], NewsResult | None] = {}
        with session_scope(session_factory) as s:
            self.account_settings(s)

    # ------------------------------------------------------------------ 공통
    @staticmethod
    def account_settings(s: Session) -> AccountSettings:
        st = s.get(AccountSettings, 1)
        if st is None:
            st = AccountSettings(id=1)
            s.add(st)
            s.flush()
        return st

    def effective_mode(self, st: AccountSettings, strat: Strategy | None = None) -> str:
        mode = st.mode
        if strat is not None and MODE_RANK[strat.exec_mode] < MODE_RANK[mode]:
            mode = strat.exec_mode
        if self.broker.is_live and not st.live_confirmed:
            mode = "notify"
        return mode

    @staticmethod
    def _activity(s: Session, kind: str, title: str, detail: str = "", decision_id: str | None = None, at: datetime | None = None) -> None:
        s.add(Activity(kind=kind, title=title, detail=detail, decision_id=decision_id, at=at or now_kst()))

    @staticmethod
    def _ledger(s: Session, kind: str, ref: str | None, payload: dict[str, Any], at: datetime | None = None) -> None:
        ledger.append(s, kind, ref, payload, at or now_kst())

    def _new_decision_id(self, s: Session, now: datetime) -> str:
        prefix = f"D-{now:%Y%m%d}-"
        n = s.scalar(select(func.count()).select_from(Decision).where(Decision.id.like(prefix + "%"))) or 0
        return f"{prefix}{n + 1:04d}"

    def account(self) -> BrokerAccount:
        return self.broker.account()

    def day_state(self, s: Session, equity: float, now: datetime) -> DayState:
        key = now.strftime("%Y-%m-%d")
        day = s.get(DayState, key)
        if day is None:
            day = DayState(date=key, start_equity=equity, alert_level=0)
            s.add(day)
            s.flush()
        return day

    def loss_state(self, s: Session, st: AccountSettings, equity: float, now: datetime) -> LossState:
        day = self.day_state(s, equity, now)
        return LossState(used=max(0.0, day.start_equity - equity), limit=float(st.daily_loss_limit), action=st.limit_action)

    def _orders_today(self, s: Session, strategy_id: int, now: datetime) -> int:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return s.scalar(
            select(func.count()).select_from(Order).where(
                Order.strategy_id == strategy_id, Order.created_at >= start, Order.status != "rejected"
            )
        ) or 0

    def _strategy_loss_today(self, s: Session, strategy_id: int, now: datetime) -> float:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        realized = s.scalar(
            select(func.coalesce(func.sum(Order.realized_pnl), 0)).where(
                Order.strategy_id == strategy_id, Order.filled_at >= start
            )
        ) or 0
        return max(0.0, -float(realized))

    def _frames(self, symbols: list[str], now: datetime, days: int = FRAME_DAYS) -> dict[str, pd.DataFrame]:
        # 장중 미완성 봉은 거래량을 장 진행 비율로 하루치로 환산해 진입 조건(거래량 배수 등)을 백테스트와 같은 기준으로 봅니다.
        # 상시 운영 시뮬레이션은 시각과 상관없이 봉을 만들므로 환산하지 않습니다.
        project = not getattr(self.market, "always_open", False)
        frames: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            try:
                ind = indicators(self.market.history(sym, days))
                frames[sym] = project_partial_bar(ind, now) if project else ind
            except Exception as exc:  # noqa: BLE001
                log.warning("시세 조회 실패 %s: %s", sym, exc)
        return frames

    def in_close_window(self, now: datetime) -> bool:
        """모멘텀·평균회귀를 판단하는 장 마감 무렵인지. 상시 운영 시뮬레이션과 빈 설정에서는 항상 참입니다."""
        window = (self.settings.close_window or "").strip()
        if not window or getattr(self.market, "always_open", False):
            return True
        start, end = (x.strip() for x in window.split("-"))
        return start <= now.strftime("%H:%M") < end

    def _bench(self) -> tuple[float | None, float | None, bool]:
        """KOSPI (오늘 등락률, 20일 변동성 %, 60일 이동평균 아래인지)."""
        try:
            b = self.market.benchmark_history(WEAK_MARKET_MA + 60)
            if len(b) < 21:
                return None, None, False
            r = b.pct_change()
            weak = len(b) >= WEAK_MARKET_MA and bool(weak_market(b).iloc[-1])
            return float(r.iloc[-1]), float(r.iloc[-20:].std() * math.sqrt(252) * 100), weak
        except Exception:  # noqa: BLE001
            return None, None, False

    def _universe(self, strat: Strategy) -> list[str]:
        """전략의 종목 범위. 코스피200 은 키움 시가총액 상위 종목(K200_UNIVERSE_SIZE)까지 넓힙니다."""
        try:
            return strategy_symbols(strat.universe, strat.custom_symbols, self.market, self.settings.k200_universe_size)
        except Exception as exc:  # noqa: BLE001
            log.warning("시가총액 상위 종목 조회 실패, 기본 목록만 씁니다: %s", exc)
            return universe_symbols(strat.universe, strat.custom_symbols)

    def _register_listing(self, now: datetime) -> None:
        """키움 종목 리스트의 이름·업종을 하루 한 번 반영합니다 (넓힌 종목 범위의 이름, 업종 편중 제한)."""
        listed = getattr(self.market, "listed_instruments", None)
        if listed is None or self._listed_on == now.date():
            return
        try:
            register(listed())
            self._listed_on = now.date()
        except Exception as exc:  # noqa: BLE001
            log.warning("종목 리스트 조회 실패: %s", exc)

    def _prefetch(self, now: datetime) -> None:
        """장 마감 판단 구간에는 넓힌 종목 범위의 일봉을 잠금 밖에서 먼저 받아 둡니다.

        키움 일봉은 장중 5분마다 다시 받아 종목이 많으면 수십 초 걸립니다. 그동안 승인·설정 요청이 막히지 않게 합니다.
        """
        if getattr(self.market, "always_open", False) or not self.market.status(now).open or not self.in_close_window(now):
            return
        with session_scope(self.session_factory) as s:
            strats = s.scalars(select(Strategy).where(Strategy.status == "running", Strategy.template.in_(CLOSE_WINDOW_TEMPLATES))).all()
        for sym in sorted({sym for strat in strats for sym in self._universe(strat)}):
            try:
                self.market.history(sym, FRAME_DAYS)
            except Exception as exc:  # noqa: BLE001
                log.debug("일봉 미리 받기 실패 %s: %s", sym, exc)

    # ------------------------------------------------------------------ 주기
    def tick(self, now: datetime | None = None) -> None:
        now = now or now_kst()
        try:
            self._prefetch(now)
            with self.lock:
                prepared = self._prepare(now)
            judged = self._judge_all(prepared)
            with self.lock:
                self._commit(judged, now)
            self.last_error = None
        except Exception as exc:  # noqa: BLE001
            log.exception("엔진 주기 실패")
            self.last_error = str(exc)[:300]
        finally:
            self.last_tick_at = now

    def _prepare(self, now: datetime) -> list[Prepared]:
        self.market.tick(now)
        with session_scope(self.session_factory) as s:
            st = self.account_settings(s)
            self._expire_pending(s, now)
            self._sync_orders(s, now)
            acct = self.account()
            loss = self._loss_guard(s, st, acct, now)
            self._snapshot(s, acct, now)
            # 시세 조회가 길어질 수 있어 장부 정리는 먼저 저장해 쓰기 잠금을 오래 잡지 않습니다.
            s.commit()
            if not st.onboarded or not st.running or not self.market.status(now).open or loss.all_halted:
                return []
            bench_ret1, bench_vol, market_weak = self._bench()
            self._register_listing(now)
            prepared: list[Prepared] = []
            # 승인 대기 중인 매수 금액은 아직 현금에 남아 있으므로 미리 빼 둡니다.
            pending_buy = sum(
                d.qty * d.price for d in s.scalars(select(Decision).where(Decision.status == "pending", Decision.side == "buy")).all()
            )
            budget = TickBudget(cash=max(0.0, acct.cash - pending_buy), claimed=set(s.scalars(select(Position.symbol)).all()))
            strats = s.scalars(select(Strategy).where(Strategy.status.in_(("running", "paused")))).all()
            for strat in strats:
                self._auto_pause(s, strat, bench_vol, now)
            # 약세장이면 모멘텀 배분 일부를 운영 중인 평균회귀 전략으로 옮깁니다 (REGIME_WEAK_SHIFT, 합계는 그대로).
            allocations = regime_allocations(
                ((x.id, x.template, x.allocation) for x in strats if x.status == "running"), market_weak, self.settings.regime_weak_shift
            )
            for strat in strats:
                # 일시정지한 전략도 보유 종목의 손절·익절·청산은 계속 관리합니다.
                prepared.extend(
                    self._prepare_strategy(
                        s, st, strat, acct, loss, bench_ret1, bench_vol, now, budget,
                        # 설정으로 꺼 둔 전략(TEMPLATE_FLAGS)은 보유 종목 손절·익절만 관리합니다.
                        allow_entries=strat.status == "running" and template_flag_note(strat.template, self.settings) is None,
                        market_weak=market_weak, allocation=allocations.get(strat.id),
                    )
                )
                s.commit()
            return prepared

    def _auto_pause(self, s: Session, strat: Strategy, bench_vol: float | None, now: datetime) -> None:
        if strat.template != "news" or bench_vol is None:
            return
        if strat.status == "running" and bench_vol > NEWS_VOLATILITY_PAUSE:
            strat.status, strat.paused_auto = "paused", True
            strat.pause_reason = f"변동성 확대로 자동 정지 · 시장 20일 변동성 {bench_vol:.0f}% · 15:30 장 마감 후 재평가"
            self._activity(s, "strategy", f"{strat.name} 전략 자동 일시정지", "시장 변동성이 설정한 조건을 넘어 신규 진입을 멈췄습니다")
        elif strat.status == "paused" and strat.paused_auto and bench_vol <= NEWS_VOLATILITY_PAUSE * 0.9:
            strat.status, strat.paused_auto, strat.pause_reason = "running", False, None
            self._activity(s, "strategy", f"{strat.name} 전략 재개", "시장 변동성이 기준 아래로 내려와 다시 실행합니다")

    def _recent_feedback(self, s: Session, strat: Strategy, symbol: str) -> list[dict[str, Any]]:
        rows = s.scalars(
            select(Decision)
            .where(Decision.status == "rejected", (Decision.symbol == symbol) | (Decision.strategy_id == strat.id))
            .order_by(Decision.created_at.desc())
            .limit(5)
        ).all()
        return [
            {"symbol": d.symbol, "side": d.side, "reason": d.reason, "at": d.created_at.strftime("%m-%d %H:%M"), "feedback": d.feedback or ""}
            for d in rows
        ]

    def _already_decided(self, s: Session, strat_id: int, symbol: str, side: str, reason: str, now: datetime) -> bool:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        pending = s.scalar(
            select(func.count()).select_from(Decision).where(Decision.symbol == symbol, Decision.status == "pending")
        )
        if pending:
            return True
        q = select(func.count()).select_from(Decision).where(
            Decision.strategy_id == strat_id,
            Decision.symbol == symbol,
            Decision.side == side,
            Decision.reason == reason,
            Decision.created_at >= start,
        )
        if side == "sell":
            # 승인 시간이 지나 취소된 매도(손절 포함)는 조건이 그대로면 다시 제안합니다.
            q = q.where(Decision.status != "expired")
        return bool(s.scalar(q))

    def _history_stat(self, strat: Strategy, reason: str, frames: dict[str, pd.DataFrame], now: datetime, full: bool = True) -> dict[str, Any] | None:
        """과거 유사 신호 통계. 종목 범위 전체 일봉(full)으로 하루 한 번 계산합니다.

        모멘텀·평균회귀는 장 마감 판단 구간 밖에서 보유 종목 일봉만 받으므로, 그때는 가장 최근에 전체로 계산한 값을 쓰고
        아직 없으면(재시작 직후) 보유 종목 몇 개로 만든 작은 표본 대신 통계를 비웁니다.
        """
        key = (strat.template, reason, strat.universe, tuple(strat.custom_symbols or ()), strat.stop_pct, strat.take_pct)
        hit = self._stat_cache.get(key)
        if hit is not None and (hit[0] == now.date() or not full):
            return hit[1]
        if not full:
            return None
        st = self.settings
        cost = 2 * (st.fee_rate + st.slippage_rate) + st.sell_tax_rate
        stat = signal_history(reason, frames, strat.stop_pct, strat.take_pct, round_trip_cost=cost)
        self._stat_cache[key] = (now.date(), stat)
        return stat

    def _chart(self, ind: pd.DataFrame, avg: float | None, levels: RiskLevels, side: str) -> dict[str, Any]:
        tail = ind.iloc[-10:]
        chart: dict[str, Any] = {
            "dates": [d.strftime("%m/%d") for d in tail.index],
            "closes": [round(float(v), 0) for v in tail["close"]],
        }
        if avg:
            chart["avg"] = round(avg, 0)
            chart["stop"] = round(avg * (1 - levels.stop_pct / 100), 0)
            chart["take"] = round(avg * (1 + levels.take_pct / 100), 0)
            chart["stop_pct"], chart["take_pct"] = levels.stop_pct, levels.take_pct
        if side == "buy" and not math.isnan(float(ind["high20_prev"].iloc[-1])):
            chart["high20"] = round(float(ind["high20_prev"].iloc[-1]), 0)
        return chart

    def _prepare_strategy(
        self,
        s: Session,
        st: AccountSettings,
        strat: Strategy,
        acct: BrokerAccount,
        loss: LossState,
        bench_ret1: float | None,
        bench_vol: float | None,
        now: datetime,
        budget: TickBudget,
        allow_entries: bool = True,
        market_weak: bool = False,
        allocation: float | None = None,
    ) -> list[Prepared]:
        out: list[Prepared] = []
        allocation = float(strat.allocation) if allocation is None else allocation
        positions = s.scalars(select(Position).where(Position.strategy_id == strat.id)).all()
        symbols = self._universe(strat)
        if strat.exclude_risky:
            try:
                risky = self.market.risky_symbols()
            except Exception as exc:  # noqa: BLE001
                log.warning("투자유의 종목 조회 실패: %s", exc)
                risky = set()
            symbols = [x for x in symbols if x not in risky]
        # 모멘텀·평균회귀의 진입과 전략 청산은 백테스트처럼 장 마감 무렵 가격으로만 판단합니다.
        # 그래서 오전 거래량이 덜 쌓여 거래량 기준을 못 넘는 문제는 생기지 않고, 이 구간에서도 아직 안 쌓인 거래량은
        # _frames 의 project_partial_bar 가 하루치로 환산합니다. 구간 설정을 비워 장중에 판단해도 같은 환산이 적용됩니다.
        close_ok = strat.template not in CLOSE_WINDOW_TEMPLATES or self.in_close_window(now)
        # 구간 밖에서는 손절·익절에 필요한 보유 종목 일봉만 받습니다 (넓힌 종목 범위를 장중 내내 받지 않도록).
        frames = self._frames(sorted((set(symbols) if close_ok else set()) | {p.symbol for p in positions}), now)
        quotes: dict[str, float] = {}
        prev_closes: dict[str, float] = {}

        def price_of(sym: str) -> float:
            if sym not in quotes:
                q = self.market.quote(sym)
                quotes[sym], prev_closes[sym] = q.price, q.prev_close
            return quotes[sym]

        orders_today = self._orders_today(s, strat.id, now)
        strategy_loss = self._strategy_loss_today(s, strat.id, now)
        invested = sum(p.qty * price_of(p.symbol) for p in positions if p.symbol in frames)
        account_ctx = {
            "equity": round(acct.equity),
            "cash": round(acct.cash),
            "loss_used_pct": round(loss.ratio * 100, 1),
            "daily_loss_limit": st.daily_loss_limit,
        }
        strategy_ctx = {
            "name": strat.name,
            "template": strat.template,
            "allocation": strat.allocation,
            "effective_allocation": round(allocation),
            "stop_pct": strat.stop_pct,
            "take_pct": strat.take_pct,
            "max_weight_pct": strat.max_weight_pct,
        }
        market_ctx = {
            "kospi_ret1_pct": None if bench_ret1 is None else round(bench_ret1 * 100, 2),
            "kospi_vol20_pct": None if bench_vol is None else round(bench_vol, 1),
            f"kospi_below_ma{WEAK_MARKET_MA}": market_weak,
        }

        # ---- 보유 종목 청산 -------------------------------------------------
        rebalance_drop: set[str] = set()
        rebalance_picks: list[tuple[str, list[Signal]]] = []
        quarter = f"{now.year}-Q{(now.month - 1) // 3 + 1}"
        week = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
        period = week if strat.template == "ml_topn" else quarter
        ml_why: str | None = None
        if allow_entries and strat.template == "ml_topn" and strat.last_rebalanced != week:
            picks_ml, ml_why = self._ml_topn_picks(symbols, now)
            if picks_ml is not None:
                rebalance_drop = {p.symbol for p in positions if p.symbol not in {sym for sym, _ in picks_ml}}
                rebalance_picks = picks_ml
            else:
                self._guard_activity(s, f"{strat.name} 리밸런싱 보류", ml_why or "", now)
        if allow_entries and strat.template == "value" and strat.last_rebalanced != quarter:
            fundamentals = {}
            for sym in frames:
                try:
                    q = self.market.quote(sym)
                    fundamentals[sym] = (q.per, q.pbr)
                    quotes[sym], prev_closes[sym] = q.price, q.prev_close
                except Exception:  # noqa: BLE001
                    pass
            ranked = value_ranking({k: v for k, v in frames.items() if k in symbols}, fundamentals)
            k = max(1, min(8, int(100 // max(strat.max_weight_pct, 1))))
            picks = ranked[:k]
            pick_syms = {p[0] for p in picks}
            rebalance_drop = {p.symbol for p in positions if p.symbol not in pick_syms}
            rebalance_picks = [(p[0], p[2]) for p in picks]

        for pos in positions:
            ind = frames.get(pos.symbol)
            if ind is None or len(ind) < 2:
                continue
            price = price_of(pos.symbol)
            pnl = price / pos.avg_price - 1 if pos.avg_price else 0.0
            hold_days = int(np.busday_count(pos.opened_at.date(), now.date()))
            row = ind.iloc[-1]
            # 진입 전날 변동성으로 정한 손절·익절·트레일링 폭이라 보유 중에는 바뀌지 않습니다.
            vol0 = vol_before(ind, pos.opened_at)
            levels = risk_levels(strat.template, strat.stop_pct, strat.take_pct, vol0)
            reason = common_exit(pnl, levels.stop_pct, levels.take_pct, pos.partial_taken)
            # 일부 익절한 뒤 남은 물량은 보유 중 최고 종가에서 일정 폭 내려오면 정리합니다 (백테스트처럼 장 마감 무렵에만 판단).
            trail = trailing_stop_pct(strat.template, strat.stop_pct, vol0)
            peak = peak_close(ind, pos.opened_at, price) if trail and pos.partial_taken else None
            if reason is None and strat.template != "value" and close_ok:
                reason = trailing_exit(trail, pos.partial_taken, price, peak) or template_exit(strat.template, row, hold_days, pnl)
            if reason is None and pos.symbol in rebalance_drop:
                reason = "ml_rebalance" if strat.template == "ml_topn" else "rebalance"
            if reason is None:
                if pos.ai_note in ("손절 제안 · 승인 대기", "매도 제안 · 승인 대기"):
                    pos.ai_note, pos.ai_note_tone = "보유 유지", "info"
                continue
            if self._already_decided(s, strat.id, pos.symbol, "sell", reason, now):
                continue
            cand = describe_exit(reason, pos.symbol, pos.name, ind, pos.avg_price, price, levels.stop_pct, levels.take_pct, bench_ret1, strat.template, peak, trail)
            self._attach_insights(cand, now)
            qty = max(1, pos.qty // 2) if reason == "take_profit" else pos.qty
            blocked = check_sell(loss, orders_today, strat.max_orders_per_day, cand.mandatory)
            if blocked:
                self._guard_activity(s, f"{pos.name} 매도 보류", blocked, now)
                continue
            out.append(
                Prepared(
                    strategy_id=strat.id,
                    candidate=cand,
                    qty=qty,
                    price=price,
                    avg_price=pos.avg_price,
                    context={
                        "strategy": strategy_ctx,
                        "position": {
                            "qty": pos.qty,
                            "avg_price": round(pos.avg_price),
                            "pnl_pct": round(pnl * 100, 2),
                            "hold_days": hold_days,
                            "stop_pct": levels.stop_pct,
                            "take_pct": levels.take_pct,
                            "partial_taken": pos.partial_taken,
                            **({"peak_close": round(peak), "trail_pct": trail} if peak else {}),
                        },
                        "account": account_ctx,
                        "market": market_ctx,
                        "user_feedback": self._recent_feedback(s, strat, pos.symbol),
                        "proposed_qty": qty,
                    },
                    chart=self._chart(ind, pos.avg_price, levels, "sell"),
                    history_stat=self._history_stat(strat, reason, frames, now, close_ok),
                    risk_rules=risk_rule_texts(strat, levels, trail),
                    risk_notes=[],
                    stop_pct=levels.stop_pct,
                    take_pct=levels.take_pct,
                    extra={"max_qty": pos.qty},
                )
            )
            orders_today += 1

        # ---- 신규 진입 -------------------------------------------------------
        if loss.buys_halted or not allow_entries:
            return out
        blocked_all = account_block(s, self.protections, now)
        if blocked_all:
            self._guard_activity(s, "보호 규칙 · 신규 매수 중지", blocked_all, now)
            return out
        held = {p.symbol: p for p in positions}
        entries: list[Candidate] = []
        if strat.template in ("momentum", "meanrev") and close_ok:
            for sym in symbols:
                if sym in budget.claimed or sym not in frames or len(frames[sym]) < 60:
                    continue
                cand = describe_entry(strat.template, sym, instrument(sym).name, frames[sym], bench_ret1)
                if cand:
                    entries.append(cand)
        elif strat.template == "news" and self.analyst.enabled:
            for sym in symbols:
                ind = frames.get(sym)
                if sym in budget.claimed or ind is None or len(ind) < 30:
                    continue
                row = ind.iloc[-1]
                daily_sigma = float(row["vol20"]) / 100 / math.sqrt(252) if not math.isnan(row["vol20"]) else 0.02
                if row["ret1"] > 0 and (row["vol_ratio"] >= 2.0 or row["ret1"] >= 2.5 * daily_sigma):
                    signals = [
                        Signal("volume", "거래량 급증", f"20일 평균 대비 {row['vol_ratio']:.1f}배", "buy", clamp((row["vol_ratio"] - 1) / 2)),
                        Signal("move", "당일 가격 변화", f"오늘 {pct(row['ret1'])} (평소 하루 변동의 {row['ret1'] / max(daily_sigma, 1e-6):.1f}배)", "buy", clamp(row["ret1"] / (4 * max(daily_sigma, 1e-6)))),
                    ]
                    entries.append(Candidate(sym, instrument(sym).name, "buy", "news", 0.5, signals, metrics={"ret1": round(float(row["ret1"]), 4), "vol_ratio": round(float(row["vol_ratio"]), 2)}))
        elif strat.template in REBALANCE_TEMPLATES:
            buy_reason = "ml_rebalance_buy" if strat.template == "ml_topn" else "rebalance_buy"
            for sym, signals in rebalance_picks:
                if sym in held or sym in budget.claimed:
                    continue
                entries.append(Candidate(sym, instrument(sym).name, "buy", buy_reason, 0.6, signals))
        # 심층 리서치가 약세(Underweight·Sell)인 종목도 막지 않고, 점수를 깎아 뒤로 보내고 아래에서 적게 삽니다.
        # ML 순위 보정(예측력이 검증된 경우만)도 정렬 전에 반영해, 같은 주기 진입 수가 모자랄 때 상위 종목이 먼저 들어갑니다.
        # 모멘텀은 기관·외국인 동반 연속 순매수(키움 ka10131)도 작은 점수 가산으로만 씁니다 (진입 조건 아님).
        flow = self._investor_flow(now) if entries and strat.template == "momentum" else {}
        haircuts: dict[str, tuple[float, float, str]] = {}
        for cand in entries:
            self._attach_insights(cand, now)
            if cand.symbol in flow:
                sig, bonus = flow_signal(*flow[cand.symbol])
                self._insert_signals(cand, [sig])
                cand.score = clamp(cand.score + bonus, 0.3, 0.9)
                cand.metrics["flow_days"] = {"institution": flow[cand.symbol][0], "foreign": flow[cand.symbol][1]}
            cut = self.insights.research_buy_haircut(cand.symbol, now.date()) if self.insights else None
            if cut:
                haircuts[cand.symbol] = cut
                cand.score = clamp(cand.score - cut[0], 0.3, 0.9)
                cand.metrics["research_size_scale"] = cut[1]
        entries.sort(key=lambda c: c.score, reverse=True)
        # 시장 약세(KOSPI 60일선 아래)면 모멘텀 신규 매수 금액을 줄입니다 (WEAK_MARKET_SIZE, 0 이면 쉼).
        market_scale = WEAK_MARKET_SIZE.get(strat.template, 1.0) if market_weak else 1.0
        if entries and market_scale <= 0:
            self._guard_activity(s, f"시장 약세 · {strat.name} 신규 매수 쉼", f"KOSPI가 {WEAK_MARKET_MA}일 이동평균 아래입니다", now)
            return out
        # 같은 업종은 MAX_POSITIONS_PER_SECTOR 종목까지만 들고 있습니다 (0 이면 제한 없음, 리밸런싱은 가치 순위를 그대로 따릅니다).
        sector_limit = self.settings.max_positions_per_sector if strat.template not in REBALANCE_TEMPLATES else 0
        sector_counts: dict[str, int] = {}
        for p in positions:
            sec = instrument(p.symbol).sector
            sector_counts[sec] = sector_counts.get(sec, 0) + 1

        taken = 0
        # 리밸런싱은 목표 종목을 한 번에 제안하고, 나머지 전략은 주기당 진입 수를 제한합니다.
        max_entries = len(entries) if strat.template in REBALANCE_TEMPLATES else MAX_ENTRIES_PER_TICK
        for cand in entries:
            if taken >= max_entries:
                break
            if cand.symbol in budget.claimed or self._already_decided(s, strat.id, cand.symbol, "buy", cand.reason, now):
                continue
            why = symbol_block(s, self.protections, cand.symbol, now)
            if why:
                self._guard_activity(s, f"{cand.name} 매수 보류", why, now)
                continue
            sector = instrument(cand.symbol).sector
            if sector_full(sector, sector_counts, sector_limit):
                self._guard_activity(s, f"{cand.name} 매수 보류", f"같은 업종({sector}) 종목을 이미 {sector_limit}개 들고 있습니다", now)
                continue
            price = price_of(cand.symbol)
            ind = frames[cand.symbol]
            vol0 = vol_before(ind, now)
            levels = risk_levels(strat.template, strat.stop_pct, strat.take_pct, vol0)
            cut = haircuts.get(cand.symbol)
            weight = strat.max_weight_pct * levels.size_scale * (cut[1] if cut else 1.0) * market_scale
            qty = size_buy(price, allocation, invested, 0.0, weight, budget.cash, self.settings.fee_rate)
            chk = check_buy(qty, price, loss, orders_today, strat.max_orders_per_day, strategy_loss, strat.daily_loss_limit, 0.0, allocation, strat.max_weight_pct)
            if chk.blocked:
                self._guard_activity(s, f"{cand.name} 매수 보류", chk.reason or "", now)
                continue
            if levels.widened:
                chk.notes.append(f"변동성이 커서 손절 −{levels.stop_pct:g}%로 넓히고 매수 금액을 {levels.size_scale * 100:.0f}%로 줄였습니다")
            if cut:
                chk.notes.append(cut[2])
            if market_scale < 1:
                chk.notes.append(f"KOSPI가 {WEAK_MARKET_MA}일 이동평균 아래라 매수 금액을 {market_scale * 100:.0f}%로 줄였습니다")
            if abs(allocation - strat.allocation) >= 1:
                chk.notes.append(f"약세장 국면 배분 · 이 전략 배분 {strat.allocation:,}원 → {allocation:,.0f}원")
            out.append(
                Prepared(
                    strategy_id=strat.id,
                    candidate=cand,
                    qty=chk.qty,
                    price=price,
                    avg_price=None,
                    context={
                        "strategy": strategy_ctx,
                        "position": None,
                        "account": account_ctx,
                        "market": market_ctx,
                        "user_feedback": self._recent_feedback(s, strat, cand.symbol),
                        "proposed_qty": chk.qty,
                    },
                    chart=self._chart(ind, None, levels, "buy"),
                    history_stat=self._history_stat(strat, cand.reason, frames, now, close_ok),
                    risk_rules=risk_rule_texts(strat, levels, trailing_stop_pct(strat.template, strat.stop_pct, vol0)),
                    risk_notes=chk.notes,
                    stop_pct=levels.stop_pct,
                    take_pct=levels.take_pct,
                    needs_news=cand.reason == "news",
                    extra={"max_qty": chk.qty, "quarter": period if strat.template in REBALANCE_TEMPLATES else None},
                )
            )
            invested += chk.qty * price
            budget.cash -= chk.qty * price * (1 + self.settings.fee_rate)
            budget.claimed.add(cand.symbol)
            sector_counts[sector] = sector_counts.get(sector, 0) + 1
            orders_today += 1
            taken += 1
        if strat.template == "value" and strat.last_rebalanced != quarter:
            strat.last_rebalanced = quarter
            self._activity(s, "strategy", f"{strat.name} {quarter} 리밸런싱", f"가치 순위 상위 {len(rebalance_picks)}종목 기준으로 매수 {taken}건 · 매도 {len(rebalance_drop)}건을 제안합니다")
        elif strat.template == "ml_topn" and allow_entries and rebalance_picks and strat.last_rebalanced != week:
            strat.last_rebalanced = week
            self._activity(s, "strategy", f"{strat.name} {week} 리밸런싱", f"ML 예측 순위 상위 {len(rebalance_picks)}종목 기준으로 매수 {taken}건 · 매도 {len(rebalance_drop)}건을 제안합니다")
        return out

    def _ml_topn_picks(self, symbols: list[str], now: datetime) -> tuple[list[tuple[str, list[Signal]]] | None, str | None]:
        """ML 상위 리밸런싱 대상 (종목 범위 안 예측 순위 상위 ML_TOPN_SIZE). ML 이 없거나 오래됐거나 검증을 못 넘으면 (None, 이유)."""
        if not self.insights:
            return None, "ML 결과를 읽을 수 없습니다"
        status = self.insights.ml_status()
        if not status or not self.insights._fresh(status.get("as_of"), self.insights.ml_max_age_days, now.date()):  # noqa: SLF001
            return None, "최근 ML 결과가 없어 리밸런싱하지 않습니다"
        metrics = status.get("metrics") or {}
        ok, why = self.insights.ml_gate(metrics)
        if not ok:
            return None, f"ML 예측력 검증 미달 · {why}"
        rows = sorted(((row["rank"], sym, row) for sym in symbols if (row := self.insights.ml_for(sym, now.date()))), key=lambda x: x[0])
        picks = []
        for rank, sym, row in rows[: max(1, self.settings.ml_topn_size)]:
            detail = f"{row['count']}종목 중 {rank}위 (상위 {row['pct'] * 100:.0f}%) · {row['as_of']} 기준 · {why}"
            picks.append((sym, [Signal("ml", "ML 랭킹 (qlib)", detail, "buy", clamp(1 - row["pct"]))]))
        return (picks or None), (None if picks else "종목 범위 안에 ML 점수가 있는 종목이 없습니다")

    def _attach_insights(self, cand: Candidate, now: datetime) -> None:
        """배치가 만든 ML 순위·심층 리서치 의견을 근거 시그널로 붙입니다 (뉴스 항목 앞)."""
        if not self.insights:
            return
        extra: list[Signal] = []
        ml = self.insights.ml_signal(cand.symbol, cand.side, now.date())
        if ml:
            sig, adjust = ml
            extra.append(sig)
            cand.score = clamp(cand.score + adjust, 0.3, 0.9)
            cand.metrics["ml_rank_pct"] = round(self.insights.ml_for(cand.symbol, now.date())["pct"] * 100, 1)
        research = self.insights.research_signal(cand.symbol, now.date())
        if research:
            extra.append(research)
        self._insert_signals(cand, extra)

    @staticmethod
    def _insert_signals(cand: Candidate, extra: list[Signal]) -> None:
        """근거 시그널을 뉴스 항목 앞에 끼웁니다."""
        if extra:
            at = next((i for i, x in enumerate(cand.signals) if x.key == "news"), len(cand.signals))
            cand.signals[at:at] = extra

    def _investor_flow(self, now: datetime) -> dict[str, tuple[int, int]]:
        get = getattr(self.market, "investor_flow", None)
        if get is None:
            return {}
        try:
            flow = get()
        except Exception as exc:  # noqa: BLE001
            log.warning("기관·외국인 수급 조회 실패: %s", exc)
            return {}
        # 수급 가산 검증용으로 그날 처음 받은 목록을 한 번 남깁니다 (app.market.flow_store).
        if flow and self._flow_saved_on != now.date():
            try:
                save_snapshot(self.settings.data_dir, flow, now, "close_window")
                self._flow_saved_on = now.date()
            except OSError as exc:
                log.warning("수급 스냅샷 저장 실패: %s", exc)
        return flow

    def _guard_activity(self, s: Session, title: str, detail: str, now: datetime) -> None:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        dup = s.scalar(
            select(func.count()).select_from(Activity).where(Activity.kind == "risk_guard", Activity.title == title, Activity.at >= start)
        )
        if not dup:
            self._activity(s, "risk_guard", title, detail)

    # ------------------------------------------------------------------ 판단
    def _judge_all(self, prepared: list[Prepared]) -> list[tuple[Prepared, AIDecision]]:
        out: list[tuple[Prepared, AIDecision]] = []
        calls = 0
        limit = self.settings.openai_max_calls_per_tick
        # 손절처럼 꼭 처리해야 하는 후보를 먼저 봅니다.
        for p in sorted(prepared, key=lambda p: (not p.candidate.mandatory, p.candidate.side != "sell", -p.candidate.score)):
            if p.needs_news:
                if calls >= limit:
                    continue
                key = (p.candidate.symbol, now_kst().strftime("%Y-%m-%d"))
                if key not in self._news_cache:
                    self._news_cache[key] = self.analyst.news_sentiment(p.candidate.symbol, p.candidate.name)
                    calls += 1
                news = self._news_cache[key]
                if news is None or news.article_count < 2 or news.score < 0.35:
                    continue
                p.news = news
                p.candidate.score = 0.45 + 0.4 * clamp(news.score)
                p.candidate.signals.insert(0, Signal("news", "뉴스·공시 감성", f"최근 기사 {news.article_count}건, 감성 {news.score:+.2f} · {news.summary}", "buy", clamp(news.score)))
            use_ai = self.analyst.enabled and calls < limit
            if use_ai:
                ai = self.analyst.judge(p.candidate, p.context | {"history_stat": p.history_stat}, p.stop_pct, p.take_pct)
                calls += 1
            else:
                ai = self.analyst._fallback(p.candidate, p.stop_pct, p.take_pct)  # noqa: SLF001
                if self.analyst.enabled:
                    ai.model = f"rule-based (주기당 AI 호출 {limit}회 초과)"
            out.append((p, ai))
        return out

    # ------------------------------------------------------------------ 반영
    def _commit(self, judged: list[tuple[Prepared, AIDecision]], now: datetime) -> None:
        if not judged:
            return
        with session_scope(self.session_factory) as s:
            st = self.account_settings(s)
            if not st.running:
                return
            acct = self.account()
            loss = self.loss_state(s, st, acct.equity, now)
            for p, ai in judged:
                strat = s.get(Strategy, p.strategy_id)
                c = p.candidate
                if strat is None or strat.status == "stopped" or (c.side == "buy" and strat.status != "running"):
                    continue
                if self._already_decided(s, strat.id, c.symbol, c.side, c.reason, now):
                    continue
                if c.side == "buy" and loss.buys_halted:
                    continue
                if loss.all_halted:
                    continue
                qty = p.qty if c.mandatory else max(1, int(p.qty * ai.quantity_ratio))
                d = Decision(
                    id=self._new_decision_id(s, now),
                    strategy_id=strat.id,
                    symbol=c.symbol,
                    name=c.name,
                    side=c.side,
                    reason=c.reason,
                    qty=min(qty, p.extra.get("max_qty", qty)),
                    max_qty=p.extra.get("max_qty", qty),
                    price=p.price,
                    avg_price=p.avg_price,
                    confidence=ai.confidence,
                    summary=ai.summary,
                    caution=ai.caution,
                    signals=ai.signals,
                    history_stat=p.history_stat,
                    risk_rules=p.risk_rules,
                    risk_notes=p.risk_notes + ([f"AI 호출 실패로 규칙 기반 설명 사용: {ai.error}"] if ai.error else []),
                    chart=p.chart | ({"news": p.news.items} if p.news else {}),
                    model=ai.model,
                    strategy_label=strategy_label(strat),
                    data_at=now,
                    mode=self.effective_mode(st, strat),
                    status="pending",
                    created_at=now,
                )
                s.add(d)
                s.flush()
                side_ko = "매수" if c.side == "buy" else "매도"
                if ai.action == "hold":
                    d.status, d.decided_by, d.decided_at = "held", "auto", now
                    self._activity(s, "ai_hold", f"{c.name} {side_ko} 보류", ai.summary, d.id)
                    self._ledger(s, "decision.held", d.id, ledger.decision_payload(d), now)
                    continue
                self._ledger(s, "decision.created", d.id, ledger.decision_payload(d), now)
                pos = s.scalar(select(Position).where(Position.strategy_id == strat.id, Position.symbol == c.symbol))
                if d.mode == "notify":
                    d.status = "notified"
                    self._activity(s, "notify", f"{c.name} {d.qty}주 {side_ko} 신호", f"{ai.summary} · 신뢰도 {round(ai.confidence * 100)}%", d.id)
                elif d.mode == "approve":
                    d.expires_at = now + timedelta(seconds=self.settings.approval_timeout_seconds)
                    self._activity(s, "approval", f"{c.name} {d.qty}주 {side_ko} 제안", ai.summary, d.id)
                    if pos and c.side == "sell":
                        pos.ai_note = "손절 제안 · 승인 대기" if c.reason == "stop_loss" else "매도 제안 · 승인 대기"
                        pos.ai_note_tone = "warn"
                else:
                    self._execute(s, d, d.qty, "auto", now)

    # ------------------------------------------------------------------ 주문
    def _execute(self, s: Session, d: Decision, qty: int, by: str, now: datetime) -> None:
        # 외부 주문 전에 지금까지의 판단 기록을 먼저 저장합니다. 브로커가 따로 DB 를 쓰는 경우의 잠금도 피합니다.
        s.commit()
        result = self.broker.place_market_order(d.symbol, d.side, qty)
        d.decided_at, d.decided_by = now, by
        side_ko = "매수" if d.side == "buy" else "매도"
        if not result.accepted:
            d.status, d.status_note = "failed", result.message
            self._activity(s, "system", f"{d.name} {side_ko} 주문 실패", result.message, d.id)
            self._ledger(s, "order.failed", d.id, {"qty": qty, "by": by, "message": result.message}, now)
            return
        d.status, d.qty = "executed", qty
        s.add(
            Order(
                decision_id=d.id,
                strategy_id=d.strategy_id,
                broker_order_no=result.order_no,
                symbol=d.symbol,
                name=d.name,
                side=d.side,
                qty=qty,
                created_at=now,
            )
        )
        s.flush()
        self._activity(s, "order_sent", f"{d.name} {qty}주 시장가 {side_ko} 주문", f"주문번호 {result.order_no} · {'승인됨' if by == 'user' else '자동 실행'}", d.id)
        self._ledger(s, "order.submitted", d.id, {"order_no": result.order_no, "symbol": d.symbol, "side": d.side, "qty": qty, "by": by, "broker": self.broker.label}, now)
        self._sync_orders(s, now)

    def _sync_orders(self, s: Session, now: datetime) -> None:
        open_orders = s.scalars(select(Order).where(Order.status.in_(OPEN_ORDER_STATES))).all()
        if not open_orders:
            return
        try:
            updates = self.broker.order_status([(o.broker_order_no, o.symbol, o.qty) for o in open_orders])
        except Exception as exc:  # noqa: BLE001
            log.warning("체결 조회 실패: %s", exc)
            return
        for o in open_orders:
            u = updates.get(o.broker_order_no)
            if u is None:
                continue
            delta = u.filled_qty - o.applied_qty
            if delta > 0 and u.avg_price:
                self._apply_fill(s, o, delta, u.avg_price, now)
                o.applied_qty += delta
            o.filled_qty = u.filled_qty
            o.fill_price = u.avg_price or o.fill_price
            o.fee, o.tax = u.fee, u.tax
            if u.state == "submitted" and o.created_at.date() < now.date():
                u.state = "cancelled"  # 전일 미체결 주문은 장 마감으로 소멸
            if u.state in ("filled", "cancelled", "rejected"):
                if u.filled_qty > 0:
                    o.status = "filled"
                    if u.filled_qty < o.qty:
                        o.message = f"{u.filled_qty}/{o.qty}주 체결 후 잔량 취소"
                    o.filled_at = now
                else:
                    o.status = u.state
                self._finalize(s, o, now)
            elif u.state == "partial":
                o.status = "partial"

    def _apply_fill(self, s: Session, o: Order, qty: int, price: float, now: datetime) -> None:
        pos = s.scalar(select(Position).where(Position.strategy_id == o.strategy_id, Position.symbol == o.symbol))
        if o.side == "buy":
            if pos:
                pos.avg_price = (pos.avg_price * pos.qty + price * qty) / (pos.qty + qty)
                pos.qty += qty
            else:
                s.add(Position(strategy_id=o.strategy_id, symbol=o.symbol, name=o.name, qty=qty, avg_price=price, opened_at=now))
            s.flush()
            return
        if pos is None:
            return
        gross = (price - pos.avg_price) * qty
        o.realized_pnl = (o.realized_pnl or 0) + gross
        o.hold_days = (now - pos.opened_at).total_seconds() / 86400
        pos.qty -= qty
        d = s.get(Decision, o.decision_id) if o.decision_id else None
        if pos.qty <= 0:
            s.delete(pos)
        else:
            if d and d.reason == "take_profit":
                pos.partial_taken = True
                pos.ai_note, pos.ai_note_tone = "일부 익절 완료", "good"
            else:
                pos.ai_note, pos.ai_note_tone = "보유 유지", "info"
        s.flush()

    def _finalize(self, s: Session, o: Order, now: datetime) -> None:
        if o.side == "sell" and o.realized_pnl is not None:
            o.realized_pnl -= o.fee + o.tax
        self._ledger(
            s,
            f"order.{o.status}",
            o.decision_id,
            {"order_no": o.broker_order_no, "symbol": o.symbol, "side": o.side, "qty": o.qty, "filled_qty": o.filled_qty, "fill_price": o.fill_price, "fee": o.fee, "tax": o.tax, "realized_pnl": o.realized_pnl},
            now,
        )
        side_ko = "매수" if o.side == "buy" else "매도"
        if o.filled_qty:
            if o.side == "buy":
                pos = s.scalar(select(Position).where(Position.strategy_id == o.strategy_id, Position.symbol == o.symbol))
                if pos and pos.ai_note not in ("일부 익절 완료",):
                    pos.ai_note, pos.ai_note_tone = "신규 진입", "info"
            d = s.get(Decision, o.decision_id) if o.decision_id else None
            detail = d.summary if d and o.side == "buy" else ""
            if o.side == "sell" and o.realized_pnl is not None:
                detail = f"실현손익 {fmt_won(o.realized_pnl, sign=True)}원 · 수수료·세금 {fmt_won(o.fee + o.tax)}원 포함"
            self._activity(s, f"{o.side}_fill", f"{o.name} {o.filled_qty}주 {side_ko} 체결 · {fmt_won(o.fill_price or 0)}원", detail, o.decision_id)
        else:
            self._activity(s, "system", f"{o.name} {side_ko} 주문 {'거부' if o.status == 'rejected' else '취소'}", o.message or "", o.decision_id)

    # ------------------------------------------------------------------ 점검
    def _expire_pending(self, s: Session, now: datetime) -> None:
        for d in s.scalars(select(Decision).where(Decision.status == "pending", Decision.expires_at < now)).all():
            d.status, d.decided_at, d.decided_by = "expired", now, "system"
            d.status_note = "응답 시간이 지나 자동 취소됐습니다"
            self._ledger(s, "decision.expired", d.id, {"expires_at": d.expires_at}, now)
            self._activity(s, "system", f"{d.name} {'매수' if d.side == 'buy' else '매도'} 제안 자동 취소", "승인 시간이 지나 주문하지 않았습니다", d.id)
            self._reset_note(s, d)
            if d.reason in ("rebalance", "rebalance_buy", "ml_rebalance", "ml_rebalance_buy"):
                # 응답이 없어 놓친 리밸런싱은 다음 거래일에 다시 제안합니다.
                strat = s.get(Strategy, d.strategy_id)
                if strat:
                    strat.last_rebalanced = None

    def _reset_note(self, s: Session, d: Decision) -> None:
        pos = s.scalar(select(Position).where(Position.strategy_id == d.strategy_id, Position.symbol == d.symbol))
        if pos and pos.ai_note_tone == "warn":
            pos.ai_note, pos.ai_note_tone = "보유 유지", "info"

    def _loss_guard(self, s: Session, st: AccountSettings, acct: BrokerAccount, now: datetime) -> LossState:
        loss = self.loss_state(s, st, acct.equity, now)
        day = self.day_state(s, acct.equity, now)
        if loss.level > day.alert_level:
            used = f"−{fmt_won(loss.used)} / −{fmt_won(loss.limit)}원"
            if loss.level == 50:
                self._activity(s, "risk_guard", "일일 손실 한도 50% 사용", f"{used} · 현재 손실을 알려드립니다")
            elif loss.level == 80:
                self._activity(s, "risk_guard", "일일 손실 한도 80% 사용", f"{used} · 신규 매수 금액을 절반으로 줄입니다")
            else:
                if st.limit_action == "all":
                    n = self._cancel_all(s, now, "일일 손실 한도 도달")
                    self._activity(s, "risk_guard", "일일 손실 한도 도달 · 모든 매매 중지", f"{used} · 미체결·승인 대기 {n}건을 취소했습니다. 다음 거래일에 다시 시작합니다")
                else:
                    self._activity(s, "risk_guard", "일일 손실 한도 도달 · 신규 매수 중지", f"{used} · 보유 종목의 손절·익절은 계속 실행합니다")
            day.alert_level = loss.level
        return loss

    def _snapshot(self, s: Session, acct: BrokerAccount, now: datetime) -> None:
        if self._last_snapshot is None:
            self._last_snapshot = s.scalar(select(func.max(EquitySnapshot.at)))
        if self._last_snapshot and now - self._last_snapshot < timedelta(minutes=5):
            return
        try:
            bench = self.market.benchmark_level()
        except Exception:  # noqa: BLE001
            bench = 0.0
        s.add(EquitySnapshot(at=now, equity=acct.equity, cash=acct.cash, benchmark=bench))
        self._last_snapshot = now

    def _cancel_all(self, s: Session, now: datetime, why: str) -> int:
        n = 0
        for d in s.scalars(select(Decision).where(Decision.status == "pending")).all():
            d.status, d.decided_at, d.decided_by, d.status_note = "cancelled", now, "system", why
            self._ledger(s, "decision.cancelled", d.id, {"why": why}, now)
            self._reset_note(s, d)
            n += 1
        for o in s.scalars(select(Order).where(Order.status.in_(OPEN_ORDER_STATES))).all():
            res = self.broker.cancel(o.broker_order_no, o.symbol)
            if res.accepted:
                o.message = f"{why}로 취소 요청"
                n += 1
        return n

    # ------------------------------------------------------------------ 사용자 동작
    def approve(self, decision_id: str, qty: int | None = None) -> Decision:
        now = now_kst()
        with self.lock, session_scope(self.session_factory) as s:
            d = s.get(Decision, decision_id)
            if d is None:
                raise EngineError("판단을 찾을 수 없습니다")
            if d.status != "pending":
                raise EngineError("이미 처리된 제안입니다")
            if d.expires_at and d.expires_at < now:
                self._expire_pending(s, now)
                raise EngineError("승인 시간이 지나 자동 취소됐습니다")
            st = self.account_settings(s)
            if not st.running:
                raise EngineError("AI가 정지된 상태입니다. 다시 시작한 뒤 승인해 주세요")
            if self.broker.is_live and not st.live_confirmed:
                raise EngineError("실전 계좌 주문이 아직 허용되지 않았습니다")
            status = self.market.status(now)
            if not status.open:
                raise EngineError(f"장이 열려 있지 않아 주문할 수 없습니다 ({status.label})")
            qty = d.qty if qty is None else int(qty)
            if qty <= 0 or qty > d.max_qty:
                raise EngineError(f"수량은 1~{d.max_qty}주 사이여야 합니다")
            acct = self.account()
            loss = self.loss_state(s, st, acct.equity, now)
            if loss.all_halted or (d.side == "buy" and loss.buys_halted):
                raise EngineError("일일 손실 한도에 도달해 주문할 수 없습니다")
            self._ledger(s, "decision.approved", d.id, {"qty": qty, "proposed_qty": d.qty}, now)
            self._execute(s, d, qty, "user", now)
            if d.status == "failed":
                raise EngineError(d.status_note or "주문이 거부됐습니다")
            return d

    def reject(self, decision_id: str, feedback: str | None = None) -> Decision:
        now = now_kst()
        with self.lock, session_scope(self.session_factory) as s:
            d = s.get(Decision, decision_id)
            if d is None:
                raise EngineError("판단을 찾을 수 없습니다")
            if d.status != "pending":
                raise EngineError("이미 처리된 제안입니다")
            d.status, d.decided_at, d.decided_by = "rejected", now, "user"
            d.feedback = (feedback or "").strip()[:500] or None
            self._ledger(s, "decision.rejected", d.id, {"feedback": d.feedback}, now)
            self._activity(s, "system", f"{d.name} {'매수' if d.side == 'buy' else '매도'} 제안 거절", "이 결정은 AI 피드백으로 기록됩니다", d.id)
            self._reset_note(s, d)
            return d

    def reopen(self, decision_id: str) -> Decision:
        """방금 거절한 제안을 승인 시간 안이라면 다시 대기 상태로 돌립니다 ('다시 검토하기')."""
        now = now_kst()
        with self.lock, session_scope(self.session_factory) as s:
            d = s.get(Decision, decision_id)
            if d is None or d.status != "rejected" or d.decided_by != "user":
                raise EngineError("되돌릴 수 없는 제안입니다")
            if d.expires_at and d.expires_at < now:
                raise EngineError("승인 시간이 지나 되돌릴 수 없습니다")
            d.status, d.decided_at, d.decided_by, d.feedback = "pending", None, None, None
            self._ledger(s, "decision.reopened", d.id, {}, now)
            return d

    def cancel_order(self, decision_id: str) -> str:
        with self.lock, session_scope(self.session_factory) as s:
            o = s.scalar(select(Order).where(Order.decision_id == decision_id).order_by(Order.id.desc()))
            if o is None:
                raise EngineError("주문이 없습니다")
            if o.status not in OPEN_ORDER_STATES:
                raise EngineError("이미 체결되었거나 끝난 주문이라 취소할 수 없습니다")
            res = self.broker.cancel(o.broker_order_no, o.symbol)
            if not res.accepted:
                raise EngineError(res.message)
            o.message = "사용자 취소 요청"
            self._activity(s, "system", f"{o.name} 주문 취소 요청", res.message, decision_id)
            self._sync_orders(s, now_kst())
            return res.message

    def stop(self) -> int:
        now = now_kst()
        with self.lock, session_scope(self.session_factory) as s:
            st = self.account_settings(s)
            st.running = False
            n = self._cancel_all(s, now, "긴급 정지")
            self._ledger(s, "control.stop", None, {"cancelled": n}, now)
            self._activity(s, "system", "긴급 정지", f"AI가 새 주문을 내지 않습니다 · 승인 대기·미체결 {n}건을 취소했습니다")
            return n

    def start(self) -> None:
        with self.lock, session_scope(self.session_factory) as s:
            st = self.account_settings(s)
            st.running = True
            self._activity(s, "system", "AI 자동매매 다시 시작", f"{MODE_LABEL[self.effective_mode(st)]} 모드로 실행합니다")
            self._ledger(s, "control.start", None, {"mode": st.mode})

    def set_mode(self, mode: str) -> None:
        if mode not in MODE_RANK:
            raise EngineError("알 수 없는 모드입니다")
        with self.lock, session_scope(self.session_factory) as s:
            st = self.account_settings(s)
            if self.broker.is_live and not st.live_confirmed and mode != "notify":
                raise EngineError("실전 계좌는 모의투자 운영 후 실전 주문을 허용해야 승인·자동 모드를 쓸 수 있습니다")
            if st.mode != mode:
                self._ledger(s, "control.mode", None, {"from": st.mode, "to": mode})
                st.mode = mode
                self._activity(s, "system", f"자동화 모드 변경 · {MODE_LABEL[mode]}", "")
