"""일봉 백테스트.

gs-quant 의 backtests 모듈처럼 '트리거 → 액션' 구조로 하루씩 진행합니다.
- 진입 트리거: signals.entry_frame (모멘텀 돌파 / 평균회귀 z-score)
- 청산 트리거: 손절·익절(common_exit), 전략 고유 청산(template_exit)
- 리밸런싱 트리거: 분기 첫 거래일 (gs-quant PeriodicTrigger 에 해당)
- 액션: 다음 거래일 시가에 시장가 체결 (슬리피지·수수료·매도 세금 반영)
신호는 종가로 판단하고 다음 날 시가에 체결해 미래 정보를 쓰지 않습니다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app.quant import timeseries as ts
from app.quant.signals import common_exit, entry_frame, template_exit


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
    partial: bool = False


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
    pending: list[tuple[str, str, int]] = []  # (symbol, side, qty) — 다음 날 시가 체결
    equity_curve: list[float] = []
    wins = losses = orders = 0
    last_quarter: tuple[int, int] | None = None
    target_value = params.amount * params.max_weight_pct / 100

    def px(sym: str, d: pd.Timestamp, col: str) -> float | None:
        f = frames[sym]
        if d not in f.index:
            return None
        v = float(f.at[d, col])
        return v if v > 0 and not math.isnan(v) else None

    for i, d in enumerate(dates):
        # 1) 전일 신호 체결 (시가)
        for sym, side, qty in pending:
            open_ = px(sym, d, "open")
            if open_ is None:
                continue
            if side == "buy":
                fill = open_ * (1 + params.slippage_rate)
                cost = fill * qty * (1 + params.fee_rate)
                if cost > cash:
                    qty = int(cash / (fill * (1 + params.fee_rate)))
                    cost = fill * qty * (1 + params.fee_rate)
                if qty <= 0:
                    continue
                cash -= cost
                p = positions.get(sym)
                if p:
                    p.avg = (p.avg * p.qty + fill * qty) / (p.qty + qty)
                    p.qty += qty
                else:
                    positions[sym] = _Pos(qty, fill, i)
            else:
                p = positions.get(sym)
                if not p:
                    continue
                qty = min(qty, p.qty)
                fill = open_ * (1 - params.slippage_rate)
                proceeds = fill * qty * (1 - params.fee_rate - params.sell_tax_rate)
                cash += proceeds
                pnl = proceeds - p.avg * qty * (1 + params.fee_rate)
                wins += pnl > 0
                losses += pnl <= 0
                p.qty -= qty
                if p.qty <= 0:
                    del positions[sym]
            orders += 1
        pending = []

        # 2) 종가 평가
        value = cash
        for sym, p in positions.items():
            close = px(sym, d, "close") or p.avg
            value += close * p.qty
        equity_curve.append(value)

        # 3) 종가 기준 신호 → 다음 날 체결 예약
        budget = params.max_orders_per_day
        for sym, p in list(positions.items()):
            f = frames[sym]
            if d not in f.index:
                continue
            row = f.loc[d]
            loc = f.index.get_loc(d)
            prev = f.iloc[loc - 1] if loc > 0 else None
            pnl = row["close"] / p.avg - 1
            reason = common_exit(pnl, params.stop_pct, params.take_pct, p.partial) or template_exit(params.template, row, prev, i - p.entry_i)
            if reason and budget > 0:
                qty = max(1, p.qty // 2) if reason == "take_profit" else p.qty
                if reason == "take_profit":
                    p.partial = True
                pending.append((sym, "sell", qty))
                budget -= 1

        if params.template == "value":
            q = (d.year, (d.month - 1) // 3)
            if q != last_quarter:
                last_quarter = q
                picks = _value_picks(frames, d, params.max_weight_pct)
                for sym in list(positions):
                    if sym not in picks and budget > 0:
                        pending.append((sym, "sell", positions[sym].qty))
                        budget -= 1
                free_cash = cash
                for sym in picks:
                    if sym in positions or budget <= 0:
                        continue
                    close = px(sym, d, "close")
                    if not close:
                        continue
                    qty = int(min(target_value, free_cash) / close)
                    if qty > 0:
                        pending.append((sym, "buy", qty))
                        free_cash -= qty * close
                        budget -= 1
        elif entries:
            cands = []
            for sym, ef in entries.items():
                if sym in positions or d not in ef.index:
                    continue
                if bool(ef.at[d, "entry"]):
                    cands.append((float(ef.at[d, "score"]), sym))
            cands.sort(reverse=True)
            free_cash = cash - sum((px(s, d, "close") or 0) * q for s, side, q in pending if side == "buy")
            for _score, sym in cands:
                if budget <= 0:
                    break
                close = px(sym, d, "close")
                if not close:
                    continue
                qty = int(min(target_value, free_cash) / close)
                if qty <= 0:
                    continue
                pending.append((sym, "buy", qty))
                free_cash -= qty * close
                budget -= 1

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
