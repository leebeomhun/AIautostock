"""일봉 백테스트.

gs-quant 의 backtests 모듈처럼 '트리거 → 액션' 구조로 하루씩 진행합니다.
- 진입 트리거: signals.entry_frame (모멘텀 돌파 / 평균회귀 z-score)
- 청산 트리거: 손절·익절(common_exit, 변동성 맞춤 폭), 부분 익절 뒤 트레일링 스톱(trailing_exit),
  전략 고유 청산·보유 기간 만료(template_exit)
- 진입 필터: 시장 약세(KOSPI 60일선 아래)면 모멘텀 매수 비중 축소, 업종당 보유 종목 수 제한(max_per_sector, 기본 끔)
- 리밸런싱 트리거: 분기 첫 거래일 (gs-quant PeriodicTrigger 에 해당)
- 액션: 그날 종가에 시장가 체결 (슬리피지·수수료·매도 세금 0.20% 반영). 상한가로 마감한 날의 종가 매수는 체결되지 않은 것으로 봅니다 (app.quant.krx)
- 결과: 정의할 수 없는 지표(매매 없는 승률, 변동 없는 샤프 등)는 0 대신 None, 데이터 부족·미반영 설정은 warnings 로 알립니다
실시간 엔진은 모멘텀·평균회귀를 장 마감 무렵(기본 15:00–15:20)에만 판단하고 바로 주문합니다.
일봉으로는 그 시점 가격을 알 수 없어 종가로 판단·체결을 근사합니다. 장중 손절은 종가 기준으로만 반영됩니다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app.quant import timeseries as ts
from app.quant.krx import PRICE_LIMIT, SELL_TAX_RATE, at_limit_up
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
    sell_tax_rate: float = SELL_TAX_RATE
    slippage_rate: float = 0.0005
    years: float = 3.0
    sectors: dict[str, str] | None = None  # 종목 → 업종
    max_per_sector: int = 0  # 같은 업종 동시 보유 종목 수 한도 (0 이면 제한 없음)
    price_limit: float = PRICE_LIMIT  # 상한가(전날 종가 +30%) 종가 매수는 체결 안 된 것으로 봅니다 (0 이면 끔)


@dataclass
class BacktestResult:
    ok: bool
    message: str | None = None
    start: str | None = None
    end: str | None = None
    curve: list[dict[str, Any]] = field(default_factory=list)
    # 정의할 수 없으면 None (구간이 하루뿐, 청산된 매매 없음, 수익률 변동 없음, 지수 시세 없음)
    cagr: float | None = 0.0
    mdd: float | None = 0.0
    win_rate: float | None = None
    trades: int = 0
    orders: int = 0
    total_return: float | None = 0.0
    benchmark_return: float | None = 0.0
    sharpe: float | None = None
    volatility: float | None = None
    worst_month: dict[str, Any] | None = None
    note: str | None = None
    warnings: list[str] = field(default_factory=list)


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
    if params.template == "ml_topn":
        return BacktestResult(False, "과거 시점의 ML 예측 순위가 없어 ML 상위 리밸런싱 전략은 백테스트하지 않습니다.")
    short = sorted(s for s, f in frames.items() if len(f) < 80)
    frames = {s: f for s, f in frames.items() if len(f) >= 80}
    if not frames or benchmark.empty:
        return BacktestResult(False, "백테스트에 쓸 시세가 부족합니다.")

    end = benchmark.index[-1]
    start = end - pd.Timedelta(days=int(params.years * 365))
    dates = benchmark.index[benchmark.index >= start]
    warnings = _data_warnings(short, frames, dates, params.years)
    limit_blocked = 0
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

    def locked(sym: str, d: pd.Timestamp, close: float) -> bool:
        nonlocal limit_blocked
        hit = _locked_up(frames[sym], d, close, params.price_limit)
        limit_blocked += hit
        return hit

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
                    if sym in positions or budget <= 0 or not close or locked(sym, d, close):
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
                if not close or sector_full(sector, held_sectors, params.max_per_sector) or locked(sym, d, close):
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

    note = "가치 전략은 과거 시점의 PER·PBR 대신 가격 대용 지표로 백테스트합니다." if params.template == "value" else None
    return _finish(equity_curve, dates, benchmark, params.amount, wins, losses, orders, note, warnings + _limit_warning(limit_blocked))


def _locked_up(frame: pd.DataFrame, d: pd.Timestamp, close: float, limit: float) -> bool:
    """그날 종가가 전날 종가 대비 상한가인지 (상한가 종가 매수는 체결되지 않은 것으로 봄)."""
    if not limit:
        return False
    k = frame.index.get_loc(d)
    if not isinstance(k, int) or k == 0:
        return False
    prev = float(frame["close"].iloc[k - 1])
    return prev > 0 and not math.isnan(prev) and at_limit_up(close, prev, limit)


def _names(syms: list[str], n: int = 5) -> str:
    return ", ".join(syms[:n]) + (f" 외 {len(syms) - n}개" if len(syms) > n else "")


def _data_warnings(short: list[str], frames: dict[str, pd.DataFrame], dates: pd.DatetimeIndex, years: float) -> list[str]:
    out = []
    if short:
        out.append(f"일봉이 80일보다 짧은 {len(short)}종목은 뺐습니다 ({_names(short)})")
    span = (dates[-1] - dates[0]).days / 365.25 if len(dates) > 1 else 0.0
    if span < years - 0.1:
        out.append(f"요청한 {years:g}년 중 지수 시세가 있는 {span:.1f}년만 계산했습니다")
    late = sorted(s for s, f in frames.items() if f.index[0] > dates[0])
    if late:
        out.append(f"{len(late)}종목은 시세가 백테스트 시작일({dates[0]:%Y-%m-%d}) 뒤부터 있어 그때부터만 반영했습니다 ({_names(late)})")
    return out


def _limit_warning(blocked: int) -> list[str]:
    return [f"상한가로 마감한 날의 매수 신호 {blocked}건은 체결되지 않은 것으로 봤습니다"] if blocked else []


def _finish(
    equity_curve: list[float],
    dates: pd.DatetimeIndex,
    benchmark: pd.Series,
    amount: float,
    wins: int,
    losses: int,
    orders: int,
    note: str | None,
    warnings: list[str] | None = None,
) -> BacktestResult:
    equity = pd.Series(equity_curve, index=dates)
    bench = benchmark.reindex(dates).ffill()
    eq_norm = ts.index(equity)
    bench_norm = ts.index(bench)
    rets = ts.returns(equity).dropna()
    vol = float(rets.std() * math.sqrt(252)) if len(rets) >= 2 else float("nan")
    days = (dates[-1] - dates[0]).days if len(dates) > 1 else 0
    bench_last = float(bench_norm.iloc[-1]) if len(bench_norm) else float("nan")
    monthly = equity.resample("ME").last().pct_change().dropna()
    worst = None
    if not monthly.empty:
        m = monthly.idxmin()
        worst = {"label": f"{m.year}년 {m.month}월", "ret": round(float(monthly.min()) * 100, 1)}
    closed = wins + losses
    warnings = list(warnings or [])
    if not closed:
        warnings.append("기간 중 청산된 매매가 없어 승률을 계산하지 않았습니다")
    step = max(1, len(dates) // 120)
    curve = [
        {"date": d.strftime("%Y-%m-%d"), "strategy": _num(eq_norm.iloc[k], 4), "benchmark": _num(bench_norm.iloc[k], 4)}
        for k, d in enumerate(dates)
        if k % step == 0 or k == len(dates) - 1
    ]
    return BacktestResult(
        ok=True,
        start=dates[0].strftime("%Y-%m-%d"),
        end=dates[-1].strftime("%Y-%m-%d"),
        curve=curve,
        cagr=_num(ts.annualized_return(equity) * 100, 1) if days > 0 and equity.iloc[0] > 0 else None,
        mdd=_num(ts.max_drawdown(equity).min() * 100, 1),
        win_rate=round(wins / closed * 100) if closed else None,
        trades=closed,
        orders=orders,
        total_return=_num((equity.iloc[-1] / amount - 1) * 100, 1),
        benchmark_return=_num((bench_last - 1) * 100, 1),
        sharpe=_num(ts.sharpe_ratio(equity), 2) if len(equity) >= 3 and vol > 0 else None,
        volatility=_num(vol * 100, 1),
        worst_month=worst,
        note=note,
        warnings=warnings,
    )


def _num(x: Any, digits: int) -> float | None:
    """반올림한 float, NaN·무한대는 None (JSON 에 NaN 이 나가지 않게)."""
    v = float(x)
    return round(v, digits) if math.isfinite(v) else None


@dataclass
class Sleeve:
    """포트폴리오 백테스트의 전략 하나. weight 는 평가자산 중 이 전략에 맡기는 비율(0–1)입니다."""

    template: str
    weight: float
    stop_pct: float
    take_pct: float
    max_weight_pct: float


def run_portfolio_backtest(
    sleeves: list[Sleeve],
    frames: dict[str, pd.DataFrame],
    benchmark: pd.Series,
    regime_shift: float = 0.0,
    max_orders_per_day: int = 20,
    fee_rate: float = 0.00015,
    sell_tax_rate: float = SELL_TAX_RATE,
    slippage_rate: float = 0.0005,
    years: float = 3.0,
    amount: float = 10_000_000,
    price_limit: float = PRICE_LIMIT,
) -> BacktestResult:
    """모멘텀·평균회귀를 현금 하나로 함께 돌리는 최소 멀티 전략 백테스트 (국면 배분 검증용).

    진입·청산 규칙은 run_backtest 와 같고, 실시간 엔진처럼 한 종목은 한 전략만 들고 전략마다 '배분 − 보유 평가금액' 안에서만 삽니다.
    배분은 그날 평가자산 × weight 이고, 약세장에서는 allocation.regime_allocations 로 모멘텀 몫을 평균회귀로 옮깁니다.
    (실시간 배분은 고정 금액이라 이익이 쌓이면 배분을 늘려야 같은 결과입니다.) 가치·뉴스 전략은 지원하지 않습니다.
    """
    from app.quant.allocation import regime_allocations

    short = sorted(s for s, f in frames.items() if len(f) < 80)
    frames = {s: f for s, f in frames.items() if len(f) >= 80}
    if not frames or benchmark.empty or not sleeves:
        return BacktestResult(False, "백테스트에 쓸 시세가 부족합니다.")
    if any(sl.template not in ("momentum", "meanrev") for sl in sleeves):
        return BacktestResult(False, "포트폴리오 백테스트는 모멘텀·평균회귀만 지원합니다.")
    end = benchmark.index[-1]
    dates = benchmark.index[benchmark.index >= end - pd.Timedelta(days=int(years * 365))]
    warnings = _data_warnings(short, frames, dates, years)
    limit_blocked = 0
    entries = [{s: entry_frame(sl.template, f) for s, f in frames.items()} for sl in sleeves]
    books: list[dict[str, _Pos]] = [{} for _ in sleeves]
    weak = weak_market(benchmark)
    cash = float(amount)
    equity_curve: list[float] = []
    wins = losses = orders = 0

    def px(sym: str, d: pd.Timestamp) -> float | None:
        f = frames[sym]
        if d not in f.index:
            return None
        v = float(f.at[d, "close"])
        return v if v > 0 and not math.isnan(v) else None

    def sell(book: dict[str, _Pos], sym: str, qty: int, close: float) -> None:
        nonlocal cash, wins, losses, orders
        p = book[sym]
        qty = min(qty, p.qty)
        proceeds = close * (1 - slippage_rate) * qty * (1 - fee_rate - sell_tax_rate)
        cash += proceeds
        p.pnl += proceeds - p.avg * qty * (1 + fee_rate)
        p.qty -= qty
        orders += 1
        if p.qty <= 0:
            wins += p.pnl > 0
            losses += p.pnl <= 0
            del book[sym]

    for i, d in enumerate(dates):
        for sl, book in zip(sleeves, books, strict=True):
            budget = max_orders_per_day
            for sym, p in list(book.items()):
                close = px(sym, d)
                if close is None or budget <= 0:
                    continue
                pnl = close / p.avg - 1
                p.peak = max(p.peak, close)
                reason = common_exit(pnl, p.stop, p.take, p.partial) or trailing_exit(p.trail, p.partial, close, p.peak) or template_exit(sl.template, frames[sym].loc[d], i - p.entry_i, pnl)
                if reason:
                    if reason == "take_profit":
                        p.partial = True
                    sell(book, sym, max(1, p.qty // 2) if reason == "take_profit" else p.qty, close)
                    budget -= 1
        mv = [sum((px(sym, d) or p.avg) * p.qty for sym, p in book.items()) for book in books]
        equity = cash + sum(mv)
        is_weak = bool(weak.get(d, False))
        alloc = regime_allocations(((k, sl.template, equity * sl.weight) for k, sl in enumerate(sleeves)), is_weak, regime_shift)
        held = {sym for book in books for sym in book}
        for k, (sl, book, ef) in enumerate(zip(sleeves, books, entries, strict=True)):
            size = WEAK_MARKET_SIZE.get(sl.template, 1.0) if is_weak else 1.0
            cands = sorted(((float(f.at[d, "score"]), sym) for sym, f in ef.items() if sym not in held and d in f.index and bool(f.at[d, "entry"])), reverse=True)
            budget = max_orders_per_day
            for _score, sym in cands:
                close = px(sym, d)
                room = alloc[k] - mv[k]
                if budget <= 0 or size <= 0 or room <= 0:
                    break
                if not close:
                    continue
                if _locked_up(frames[sym], d, close, price_limit):
                    limit_blocked += 1
                    continue
                vol = vol_before(frames[sym], d)
                levels = risk_levels(sl.template, sl.stop_pct, sl.take_pct, vol)
                fill = close * (1 + slippage_rate)
                value = min(alloc[k] * sl.max_weight_pct / 100 * levels.size_scale * size, room, cash)
                qty = int(value / (fill * (1 + fee_rate)))
                if qty <= 0:
                    continue
                cash -= fill * qty * (1 + fee_rate)
                book[sym] = _Pos(qty, fill, i, levels.stop_pct, levels.take_pct, trailing_stop_pct(sl.template, sl.stop_pct, vol), close)
                mv[k] += close * qty
                held.add(sym)
                orders += 1
                budget -= 1
        equity_curve.append(cash + sum(sum((px(sym, d) or p.avg) * p.qty for sym, p in book.items()) for book in books))
    return _finish(equity_curve, dates, benchmark, amount, wins, losses, orders, None, warnings + _limit_warning(limit_blocked))


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
