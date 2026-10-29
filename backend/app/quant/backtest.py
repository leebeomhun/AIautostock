"""일봉 백테스트.

gs-quant 의 backtests 모듈처럼 '트리거 → 액션' 구조로 하루씩 진행합니다.
- 진입 트리거: signals.entry_frame (모멘텀 돌파 / 평균회귀 z-score)
- 청산 트리거: 손절·익절(common_exit, 변동성 맞춤 폭), 부분 익절 뒤 트레일링 스톱(trailing_exit),
  전략 고유 청산·보유 기간 만료(template_exit)
- 진입 필터: 시장 약세(KOSPI 60일선 아래)면 모멘텀 매수 비중 축소, 업종당 보유 종목 수 제한(max_per_sector, 기본 끔)
- 리밸런싱 트리거: 분기 첫 거래일 (gs-quant PeriodicTrigger 에 해당)
- 액션: 그날 종가에 시장가 체결 (슬리피지·수수료·매도 세금 반영)
실시간 엔진은 모멘텀·평균회귀를 장 마감 무렵(기본 15:00–15:20)에만 판단하고 바로 주문합니다.
일봉으로는 그 시점 가격을 알 수 없어 종가로 판단·체결을 근사합니다. 장중 손절은 종가 기준으로만 반영됩니다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app.quant import timeseries as ts
from app.quant.signals import (
    WEAK_MARKET_SIZE,
    RiskLevels,
    common_exit,
    entry_frame,
    risk_levels,
    sector_full,
    template_exit,
    trailing_exit,
    trailing_stop_pct,
    vol_before,
    weak_market,
)


@dataclass
class BacktestParams:
    template: str
    amount: float
    stop_pct: float
    take_pct: float
    max_weight_pct: float
    max_orders_per_day: int = 20
    fee_rate: float = 0.00015
    sell_tax_rate: float = 0.0018
    slippage_rate: float = 0.0005
    years: float = 3.0
    sectors: dict[str, str] | None = None  # 종목 → 업종
    max_per_sector: int = 0  # 같은 업종 동시 보유 종목 수 한도 (0 이면 제한 없음)


@dataclass
class BacktestResult:
    ok: bool
    message: str | None = None
    start: str | None = None
    end: str | None = None
    curve: list[dict[str, Any]] = field(default_factory=list)
    cagr: float = 0.0
    mdd: float = 0.0
    win_rate: float = 0.0
    trades: int = 0
    orders: int = 0
    total_return: float = 0.0
    benchmark_return: float = 0.0
    sharpe: float = 0.0
    volatility: float = 0.0
    worst_month: dict[str, Any] | None = None
    note: str | None = None


@dataclass
class _Pos:
    qty: int
    avg: float
    entry_i: int
    stop: float
    take: float
    trail: float | None = None
    peak: float = 0.0  # 보유 중 최고 종가
    partial: bool = False
    pnl: float = 0.0  # 부분 매도까지 합친 실현손익


def run_backtest(params: BacktestParams, frames: dict[str, pd.DataFrame], benchmark: pd.Series) -> BacktestResult:
    if params.template == "news":
        return BacktestResult(False, "과거 시점의 뉴스 데이터가 없어 AI 뉴스 감성 전략은 백테스트하지 않습니다. 모의투자로 먼저 확인해 보세요.")
    frames = {s: f for s, f in frames.items() if len(f) >= 80}
    if not frames or benchmark.empty:
        return BacktestResult(False, "백테스트에 쓸 시세가 부족합니다.")

    end = benchmark.index[-1]
    start = end - pd.Timedelta(days=int(params.years * 365))
    dates = benchmark.index[benchmark.index >= start]
    # 이동평균이 안정되도록 시작일 이전 데이터는 지표 계산에만 씁니다.
    entries = {s: entry_frame(params.template, f) for s, f in frames.items()} if params.template in ("momentum", "meanrev") else {}
    cash = float(params.amount)
    positions: dict[str, _Pos] = {}
    equity_curve: list[float] = []
    wins = losses = orders = 0
    last_quarter: tuple[int, int] | None = None
    target_value = params.amount * params.max_weight_pct / 100
    weak = weak_market(benchmark)
    sectors = params.sectors or {}

    def px(sym: str, d: pd.Timestamp, col: str) -> float | None:
        f = frames[sym]
        if d not in f.index:
            return None
        v = float(f.at[d, col])
        return v if v > 0 and not math.isnan(v) else None

    def sell(sym: str, qty: int, close: float) -> None:
        nonlocal cash, wins, losses, orders
        p = positions[sym]
        qty = min(qty, p.qty)
        fill = close * (1 - params.slippage_rate)
        proceeds = fill * qty * (1 - params.fee_rate - params.sell_tax_rate)
        cash += proceeds
        p.pnl += proceeds - p.avg * qty * (1 + params.fee_rate)
        p.qty -= qty
        orders += 1
        if p.qty <= 0:
            # 승률은 부분 매도를 합쳐 매매 한 건(진입~전량 청산) 단위로 셉니다.
            wins += p.pnl > 0
            losses += p.pnl <= 0
            del positions[sym]

    def buy(sym: str, value: float, close: float, i: int, levels: RiskLevels, trail: float | None = None) -> bool:
        nonlocal cash, orders
        fill = close * (1 + params.slippage_rate)
        qty = int(min(value, cash) / (fill * (1 + params.fee_rate)))
        if qty <= 0:
            return False
        cash -= fill * qty * (1 + params.fee_rate)
        positions[sym] = _Pos(qty, fill, i, levels.stop_pct, levels.take_pct, trail, close)
        orders += 1
        return True

    for i, d in enumerate(dates):
        # 실시간 엔진은 장 마감 무렵(15:00–15:20)에 판단하고 바로 주문하므로, 종가로 판단해 같은 종가에 체결합니다.
        budget = params.max_orders_per_day
        for sym, p in list(positions.items()):
            close = px(sym, d, "close")
            if close is None or budget <= 0:
                continue
            row = frames[sym].loc[d]
            pnl = close / p.avg - 1
            p.peak = max(p.peak, close)
            reason = (
                common_exit(pnl, p.stop, p.take, p.partial)
                or trailing_exit(p.trail, p.partial, close, p.peak)
                or template_exit(params.template, row, i - p.entry_i, pnl)
            )
            if reason:
                if reason == "take_profit":
                    p.partial = True
                sell(sym, max(1, p.qty // 2) if reason == "take_profit" else p.qty, close)
                budget -= 1

        if params.template == "value":
            q = (d.year, (d.month - 1) // 3)
            if q != last_quarter:
                last_quarter = q
                picks = _value_picks(frames, d, params.max_weight_pct)
                for sym in list(positions):
                    close = px(sym, d, "close")
                    if sym not in picks and budget > 0 and close:
                        sell(sym, positions[sym].qty, close)
                        budget -= 1
                for sym in picks:
                    close = px(sym, d, "close")
                    if sym in positions or budget <= 0 or not close:
                        continue
                    if buy(sym, target_value, close, i, RiskLevels(params.stop_pct, params.take_pct, 1.0)):
                        budget -= 1
        elif entries:
            size = WEAK_MARKET_SIZE.get(params.template, 1.0) if bool(weak.get(d, False)) else 1.0
            held_sectors: dict[str, int] = {}
            for sym in positions:
                held_sectors[sectors.get(sym, "")] = held_sectors.get(sectors.get(sym, ""), 0) + 1
            cands = []
            for sym, ef in entries.items():
                if sym in positions or d not in ef.index:
                    continue
                if bool(ef.at[d, "entry"]):
                    cands.append((float(ef.at[d, "score"]), sym))
            cands.sort(reverse=True)
            for _score, sym in cands:
                if budget <= 0 or size <= 0:
                    break
                close = px(sym, d, "close")
                sector = sectors.get(sym)
                if not close or sector_full(sector, held_sectors, params.max_per_sector):
                    continue
                vol = vol_before(frames[sym], d)
                levels = risk_levels(params.template, params.stop_pct, params.take_pct, vol)
                if buy(sym, target_value * levels.size_scale * size, close, i, levels, trailing_stop_pct(params.template, params.stop_pct, vol)):
                    budget -= 1
                    if sector:
                        held_sectors[sector] = held_sectors.get(sector, 0) + 1

        value = cash
        for sym, p in positions.items():
            value += (px(sym, d, "close") or p.avg) * p.qty
        equity_curve.append(value)

    equity = pd.Series(equity_curve, index=dates)
    bench = benchmark.reindex(dates).ffill()
    eq_norm = ts.index(equity)
    bench_norm = ts.index(bench)
    monthly = equity.resample("ME").last().pct_change().dropna()
    worst = None
    if not monthly.empty:
        m = monthly.idxmin()
        worst = {"label": f"{m.year}년 {m.month}월", "ret": round(float(monthly.min()) * 100, 1)}
    closed = wins + losses
    step = max(1, len(dates) // 120)
    curve = [
        {"date": d.strftime("%Y-%m-%d"), "strategy": round(float(eq_norm.iloc[k]), 4), "benchmark": round(float(bench_norm.iloc[k]), 4)}
        for k, d in enumerate(dates)
        if k % step == 0 or k == len(dates) - 1
    ]
    return BacktestResult(
        ok=True,
        start=dates[0].strftime("%Y-%m-%d"),
        end=dates[-1].strftime("%Y-%m-%d"),
        curve=curve,
        cagr=round(ts.annualized_return(equity) * 100, 1),
        mdd=round(float(ts.max_drawdown(equity).min()) * 100, 1),
        win_rate=round(wins / closed * 100) if closed else 0,
        trades=closed,
        orders=orders,
        total_return=round((float(equity.iloc[-1]) / params.amount - 1) * 100, 1),
        benchmark_return=round((float(bench_norm.iloc[-1]) - 1) * 100, 1),
        sharpe=round(ts.sharpe_ratio(equity), 2),
        volatility=round(float(ts.returns(equity).std() * math.sqrt(252) * 100), 1),
        worst_month=worst,
        note="가치 전략은 과거 시점의 PER·PBR 대신 가격 대용 지표로 백테스트합니다." if params.template == "value" else None,
    )


def _value_picks(frames: dict[str, pd.DataFrame], d: pd.Timestamp, max_weight_pct: float) -> list[str]:
    k = max(1, min(8, int(100 // max(max_weight_pct, 1))))
    scored = []
    for sym, f in frames.items():
        if d not in f.index:
            continue
        row = f.loc[d]
        if math.isnan(row["ma250"]) or math.isnan(row["vol20"]) or f.index.get_loc(d) < 120:
            continue
        discount = row["close"] / row["ma250"] - 1
        scored.append((-discount * 3 - row["vol20"] / 40, sym))
    scored.sort(reverse=True)
    return [s for _, s in scored[:k]]
