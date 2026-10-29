"""전략별 진입·청산 시그널.

퀀트 규칙이 후보와 근거 수치를 만들고, AI(OpenAI)는 그 근거를 읽어 최종 판단과 설명을 붙입니다.
진입 규칙은 백테스트와 실시간이 같은 벡터 연산(entry_frame)을 씁니다.
평균회귀 진입은 gs-quant MeanReversionTrigger 와 같은 z-score 경계(기본 2.0)를 씁니다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from app.quant import timeseries as ts

TEMPLATES: dict[str, dict[str, Any]] = {
    "momentum": {
        "name": "모멘텀 돌파",
        "desc": "오르기 시작한 종목의 흐름을 따라 삽니다",
        "hold": "3–10일",
        "freq": "주 4–6회",
        "risk": "높음",
        "version": "v2.0",
        "model_id": "momentum",
    },
    "meanrev": {
        "name": "평균회귀 스윙",
        "desc": "짧은 기간 많이 빠진 종목을 반등 구간에 삽니다",
        "hold": "2–5일",
        "freq": "주 6–10회",
        "risk": "중간",
        "version": "v1.3",
        "model_id": "mean-rev",
    },
    "news": {
        "name": "AI 뉴스 감성",
        "desc": "공시·뉴스 분위기 변화를 매매 신호로 씁니다",
        "hold": "1–3일",
        "freq": "주 8–15회",
        "risk": "높음",
        "version": "v1.0",
        "model_id": "news-sent",
    },
    "value": {
        "name": "배당·가치 리밸런싱",
        "desc": "PER·PBR이 낮은 종목 비중을 분기마다 다시 맞춥니다",
        "hold": "약 3개월",
        "freq": "분기 1회",
        "risk": "낮음",
        "version": "v1.0",
        "model_id": "value-rebal",
    },
}

Z_SCORE_BOUND = 2.0
BREAKOUT_VOLUME_RATIO = 1.5
NEWS_VOLATILITY_PAUSE = 28.0  # 시장 20일 변동성(연환산 %)이 이 값을 넘으면 뉴스 전략을 멈춥니다
NEWS_MAX_HOLD_DAYS = 3
# 보유 기간 청산: (거래일, 최소 수익률). 그 기간이 지나도 수익률이 기준 미만이면 정리하고, None 이면 무조건 정리합니다.
# 모멘텀은 무조건 정리하면 강하게 오르는 종목까지 잘라 최근 1–2년 성과가 나빠져서, 오르지 못한 종목만 정리합니다.
# 평균회귀는 보유 기간 청산을 넣으면 백테스트 성과가 나빠져 넣지 않았습니다.
TIME_EXITS: dict[str, tuple[int, float | None]] = {"momentum": (10, 0.03), "news": (NEWS_MAX_HOLD_DAYS, None)}
# 장 마감 무렵에만 진입·전략 청산을 판단하는 전략. 백테스트와 같은 시점(종가)에 맞춥니다.
CLOSE_WINDOW_TEMPLATES = ("momentum", "meanrev")
# 변동성에 맞춰 손절폭과 매수 비중을 정하는 전략. 모멘텀은 변동성 큰 주도주 비중이 줄어 성과가 나빠져 빼 두었습니다.
VOL_STOP_TEMPLATES = ("meanrev",)
# 손절폭 = 진입 전날 20일 일간 변동성 × 이 배수. 설정 손절 %보다 좁아지지 않고 그 2배를 넘지 않습니다.
STOP_VOL_MULT = 2.5
# 정규장(09:00–15:30) 시작 후 분 → 하루 거래량 중 그때까지 체결됐을 누적 비율 (대략값).
# 장 초반과 종가 동시호가(15:20–15:30)에 거래가 몰리는 U자형이라 단순 시간 비율보다 오전 비율이 큽니다.
INTRADAY_VOLUME_CURVE = ((0, 0.0), (30, 0.17), (60, 0.27), (120, 0.41), (180, 0.52), (240, 0.62), (300, 0.73), (360, 0.86), (380, 0.9), (390, 1.0))
# 장 시작 직후 몇 분치 거래량을 하루치로 크게 부풀리지 않도록 환산 비율의 하한을 둡니다.
MIN_VOLUME_FRACTION = 0.1


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return lo
    return max(lo, min(hi, float(x)))


def strength_label(x: float) -> str:
    if x >= 0.7:
        return "강함"
    if x >= 0.4:
        return "중간"
    if x >= 0.1:
        return "약함"
    return "없음"


def pct(x: float, digits: int = 2, sign: bool = True) -> str:
    v = x * 100
    s = f"{abs(v):.{digits}f}%"
    if not sign:
        return s
    return ("+" if v > 0 else "−" if v < 0 else "") + s


@dataclass
class Signal:
    key: str
    name: str
    detail: str
    side: str  # buy | sell | hold | neutral
    strength: float

    def as_dict(self) -> dict[str, Any]:
        s = round(clamp(self.strength), 2)
        return {
            "key": self.key,
            "name": self.name,
            "detail": self.detail,
            "side": self.side,
            "strength": s,
            "label": strength_label(s),
        }


@dataclass
class Candidate:
    symbol: str
    name: str
    side: str  # buy | sell
    reason: str  # breakout | meanrev_entry | meanrev_exit | trend_break | stop_loss | take_profit | news | rebalance | time_exit
    score: float
    signals: list[Signal]
    qty_fraction: float = 1.0
    mandatory: bool = False  # 손절처럼 AI 가 보류할 수 없는 규칙
    metrics: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 지표


def indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    out = pd.DataFrame(index=df.index)
    out["open"] = df["open"].astype(float)
    out["close"] = close
    out["volume"] = volume
    out["ma20"] = ts.moving_average(close, ts.Window(20, 0))
    out["ma60"] = ts.moving_average(close, ts.Window(60, 0))
    out["ma250"] = ts.moving_average(close, ts.Window(250, 0))
    out["high20_prev"] = close.rolling(20, min_periods=20).max().shift(1)
    out["vol_avg20_prev"] = volume.rolling(20, min_periods=10).mean().shift(1)
    out["vol_avg5_prev"] = volume.rolling(5, min_periods=3).mean().shift(1)
    out["vol_ratio"] = volume / out["vol_avg20_prev"]
    out["vol_change5"] = volume / out["vol_avg5_prev"] - 1
    out["rsi14"] = ts.relative_strength_index(close, 14).reindex(df.index)
    out["z20"] = ts.zscores(close, ts.Window(20, 0)).reindex(df.index)
    out["vol20"] = ts.volatility(close, ts.Window(20, 0)).reindex(df.index)
    out["ret1"] = ts.returns(close, 1)
    out["ret10"] = ts.returns(close, 10)
    out["ret60"] = ts.returns(close, 60)
    out["fwd5"] = close.shift(-5) / close - 1
    return out


def session_volume_fraction(now: datetime) -> float:
    """정규장 중 now 까지 하루 거래량의 몇 %가 체결됐을지 (INTRADAY_VOLUME_CURVE 보간). 장 시작 전·마감 뒤는 1."""
    minutes = now.hour * 60 + now.minute + now.second / 60 - 9 * 60
    if minutes <= 0 or minutes >= 390:
        return 1.0
    for (m0, f0), (m1, f1) in zip(INTRADAY_VOLUME_CURVE, INTRADAY_VOLUME_CURVE[1:]):
        if minutes <= m1:
            return max(MIN_VOLUME_FRACTION, f0 + (f1 - f0) * (minutes - m0) / (m1 - m0))
    return 1.0


def project_partial_bar(ind: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """장중 미완성 봉(오늘 날짜의 마지막 행)의 거래량 비율을 하루치로 환산합니다.

    vol_ratio 는 '오늘 누적 거래량 / 직전 20일 평균'이라 장 초반에는 거래량 1.5배 조건을 거의 넘지 못합니다.
    지금까지 체결됐을 비율로 나눠 하루치로 늘리면 백테스트(완성 봉)와 같은 기준으로 비교할 수 있습니다.
    모멘텀·평균회귀 진입은 지금 15:00–15:20(CLOSE_WINDOW)에만 판단하는데, 그때도 하루 거래량의 85–90%만 쌓여
    있어 환산하지 않으면 백테스트보다 조금 엄격합니다. 장중 신호(뉴스 전략, 구간 설정을 비운 경우)는 이 환산이 없으면
    오전에 신호가 거의 나지 않습니다.
    """
    fraction = session_volume_fraction(now)
    if fraction >= 1 or ind.empty or ind.index[-1].date() != now.date():
        return ind
    out = ind.copy()
    i = out.index[-1]
    projected = float(out.at[i, "volume"]) / fraction
    out.at[i, "vol_ratio"] = projected / out.at[i, "vol_avg20_prev"]
    out.at[i, "vol_change5"] = projected / out.at[i, "vol_avg5_prev"] - 1
    out["vol_fraction"] = 1.0
    out.at[i, "vol_fraction"] = fraction
    return out


def entry_frame(template: str, ind: pd.DataFrame) -> pd.DataFrame:
    """진입 여부(entry)와 퀀트 점수(score, 0–1)를 날짜별로 계산합니다."""
    c = ind["close"]
    if template == "momentum":
        margin = (c / ind["high20_prev"] - 1).clip(lower=0)
        s1 = (margin / 0.03).clip(0, 1)
        s_vol = ((ind["vol_ratio"] - 1) / 2).clip(0, 1)
        s_trend = ((c / ind["ma60"] - 1) / 0.1).clip(0, 1)
        overbought = ((ind["rsi14"] - 70) / 30).clip(0, 1)
        score = (0.45 + 0.25 * s1 + 0.2 * s_vol + 0.1 * s_trend - 0.15 * overbought).clip(0.3, 0.9)
        entry = (c > ind["high20_prev"]) & (ind["vol_ratio"] >= BREAKOUT_VOLUME_RATIO) & (c > ind["ma60"])
    elif template == "meanrev":
        s_z = ((-ind["z20"] - Z_SCORE_BOUND) / 1.5).clip(0, 1)
        s_rsi = ((35 - ind["rsi14"]) / 15).clip(0, 1)
        s_reb = (ind["ret1"] / 0.02).clip(0, 1)
        trend = np.where(c > ind["ma250"], 0.05, -0.1)
        score = (0.45 + 0.2 * s_z + 0.15 * s_rsi + 0.1 * s_reb + trend).clip(0.3, 0.9)
        # 직전일에 z-score 경계를 넘었고 오늘 반등이 시작된 경우
        entry = (ind["z20"].shift(1) <= -Z_SCORE_BOUND) & (ind["rsi14"].shift(1) < 35) & (ind["ret1"] > 0)
    else:
        score = pd.Series(0.0, index=ind.index)
        entry = pd.Series(False, index=ind.index)
    return pd.DataFrame({"entry": entry.fillna(False).astype(bool), "score": pd.Series(score, index=ind.index).fillna(0)})


def template_exit(template: str, row: pd.Series, hold_days: int, pnl_pct: float) -> str | None:
    """보유 중 전략 고유 청산 조건. 손절·익절은 common_exit 에서 따로 봅니다."""
    if template == "momentum":
        # 이탈한 날 하루만 보면 그날 판단을 보류·거절했을 때 다시는 청산 신호가 나지 않습니다.
        if row["close"] < row["ma20"]:
            return "trend_break"
    elif template == "meanrev":
        # gs-quant MeanReversionTrigger: 롱 포지션은 가격이 롤링 평균 위로 올라오면 청산
        if row["close"] >= row["ma20"] and hold_days >= 1:
            return "meanrev_exit"
    days, min_pnl = TIME_EXITS.get(template, (0, None))
    if days and hold_days >= days and (min_pnl is None or pnl_pct < min_pnl):
        return "time_exit"
    return None


def common_exit(pnl_pct: float, stop_pct: float, take_pct: float, partial_taken: bool) -> str | None:
    if pnl_pct <= -stop_pct / 100:
        return "stop_loss"
    if pnl_pct >= take_pct / 100 and not partial_taken:
        return "take_profit"
    return None


@dataclass
class RiskLevels:
    stop_pct: float
    take_pct: float
    size_scale: float  # 종목 비중 한도에 곱하는 값 (0.5–1)

    @property
    def widened(self) -> bool:
        return self.size_scale < 0.999


def risk_levels(template: str, stop_pct: float, take_pct: float, vol20_prev: float | None) -> RiskLevels:
    """변동성에 맞춘 손절·익절폭과 매수 비중.

    변동성이 큰 종목은 손절폭을 넓히고(설정값의 최대 2배) 그만큼 적게 삽니다.
    그래서 한 번 손절로 잃는 돈은 갭 하락이 아니면 어느 종목이든 '배분 × 종목 비중 × 설정 손절 %' 안쪽입니다.
    vol20_prev 는 진입 전날 20일 변동성(연환산 %)으로, 진입 후에도 바뀌지 않게 날짜로 고정해 씁니다.
    """
    if template not in VOL_STOP_TEMPLATES or vol20_prev is None or math.isnan(vol20_prev) or vol20_prev <= 0:
        return RiskLevels(stop_pct, take_pct, 1.0)
    daily = vol20_prev / math.sqrt(252)
    stop = min(stop_pct * 2, max(stop_pct, STOP_VOL_MULT * daily))
    k = stop / stop_pct
    return RiskLevels(round(stop, 2), round(take_pct * k, 2), round(1 / k, 4))


def vol_before(ind: pd.DataFrame, day: pd.Timestamp) -> float | None:
    """day 전 거래일의 20일 변동성. 장중의 미완성 봉이나 진입 이후 값은 쓰지 않습니다."""
    past = ind["vol20"][ind.index < pd.Timestamp(day).normalize()]
    return float(past.iloc[-1]) if len(past) else None


# ---------------------------------------------------------------------------
# 근거 문장


def _market_signal(bench_ret1: float | None, stock_ret1: float, side: str) -> Signal:
    if bench_ret1 is None or math.isnan(bench_ret1):
        return Signal("market", "시장 흐름", "지수 데이터 없음 · 판단에 반영 안 함", "neutral", 0.0)
    arrow = "▲" if bench_ret1 >= 0 else "▼"
    rel = "종목만 약세" if stock_ret1 < 0 <= bench_ret1 else "시장과 같은 방향" if stock_ret1 * bench_ret1 > 0 else "종목만 강세"
    detail = f"KOSPI 오늘 {arrow}{abs(bench_ret1) * 100:.1f}%, {rel}"
    if side == "buy":
        return Signal("market", "시장 흐름", detail, "buy" if bench_ret1 > 0 else "hold", clamp(abs(bench_ret1) / 0.015) * 0.5)
    # 매도 판단에서 시장이 오르면 반등 여지가 있다는 보유 쪽 근거
    return Signal("market", "시장 흐름", detail, "hold" if bench_ret1 > 0 else "sell", clamp(abs(bench_ret1) / 0.015) * 0.5)


def _news_placeholder() -> Signal:
    return Signal("news", "뉴스·공시 감성", "뉴스 분석 안 함 · 판단에 반영 안 함", "neutral", 0.0)


def describe_entry(template: str, symbol: str, name: str, ind: pd.DataFrame, bench_ret1: float | None) -> Candidate | None:
    frame = entry_frame(template, ind)
    if len(frame) < 2 or not bool(frame["entry"].iloc[-1]):
        return None
    row = ind.iloc[-1]
    score = float(frame["score"].iloc[-1])
    signals: list[Signal] = []
    if template == "momentum":
        margin = row["close"] / row["high20_prev"] - 1
        signals.append(Signal("breakout", "20일 고점 돌파", f"직전 20일 고점 {row['high20_prev']:,.0f}원보다 {pct(margin)}", "buy", 0.5 + clamp(margin / 0.03) * 0.5))
        signals.append(Signal("volume", "거래량 증가", f"20일 평균 대비 {row['vol_ratio']:.1f}배{_partial_note(row)}", "buy", clamp((row["vol_ratio"] - 1) / 2)))
        signals.append(Signal("trend", "중기 추세", f"60일 이동평균보다 {pct(row['close'] / row['ma60'] - 1)} 위", "buy", clamp((row["close"] / row["ma60"] - 1) / 0.1)))
        rsi = row["rsi14"]
        if rsi >= 70:
            signals.append(Signal("rsi", f"RSI(14) {rsi:.0f}", "과매수(70) 구간, 단기 되돌림 가능", "hold", clamp((rsi - 70) / 30 + 0.3)))
        else:
            signals.append(Signal("rsi", f"RSI(14) {rsi:.0f}", "과매수 전 구간", "buy", 0.2))
        reason = "breakout"
    else:
        z_prev, rsi_prev = float(ind["z20"].iloc[-2]), float(ind["rsi14"].iloc[-2])
        signals.append(Signal("zscore", f"전일 20일 z-score {z_prev:.2f}", f"전일 종가가 20일 평균보다 {abs(z_prev):.1f} 표준편차 아래 (경계 −{Z_SCORE_BOUND:.1f}) · 오늘 반등으로 {row['z20']:.2f}", "buy", 0.5 + clamp((-z_prev - 2) / 1.5) * 0.5))
        signals.append(Signal("rsi", f"전일 RSI(14) {rsi_prev:.0f}", f"과매도에 가까운 구간(35 미만)에서 오늘 {row['rsi14']:.0f}로 반등 시작", "buy", clamp((35 - rsi_prev) / 15)))
        signals.append(Signal("rebound", "반등 확인", f"오늘 {pct(row['ret1'])}", "buy", clamp(row["ret1"] / 0.02)))
        if row["close"] > row["ma250"]:
            signals.append(Signal("longtrend", "장기 추세", f"250일 평균보다 {pct(row['close'] / row['ma250'] - 1)} 위", "buy", 0.3))
        else:
            signals.append(Signal("longtrend", "장기 추세", f"250일 평균보다 {pct(row['close'] / row['ma250'] - 1)} 아래, 하락 추세 속 반등", "hold", 0.45))
        reason = "meanrev_entry"
    signals.append(_market_signal(bench_ret1, float(row["ret1"]), "buy"))
    signals.append(_news_placeholder())
    metrics = _metrics(row)
    if template == "meanrev":
        # 진입 조건은 전일 값으로 판단하므로 AI 에게도 두 시점을 구분해 넘깁니다.
        metrics |= {"z20_prev_day": round(float(ind["z20"].iloc[-2]), 2), "rsi14_prev_day": round(float(ind["rsi14"].iloc[-2]), 2), "entry_rule": "전일 z20<=-2 그리고 전일 RSI<35, 오늘 상승"}
    return Candidate(symbol, name, "buy", reason, score, signals, metrics=metrics)


def describe_exit(
    reason: str,
    symbol: str,
    name: str,
    ind: pd.DataFrame,
    avg_price: float,
    price: float,
    stop_pct: float,
    take_pct: float,
    bench_ret1: float | None,
    template: str | None = None,
) -> Candidate:
    row = ind.iloc[-1]
    pnl = price / avg_price - 1 if avg_price else 0.0
    signals: list[Signal] = []
    qty_fraction = 1.0
    mandatory = False
    if reason == "stop_loss":
        over = (-pnl - stop_pct / 100) / (stop_pct / 100)
        signals.append(Signal("stop", "손절 기준 도달", f"평균단가 대비 {pct(pnl)} (기준 −{stop_pct:.1f}%)", "sell", 0.75 + clamp(over * 3) * 0.25))
        mandatory = True
        score = 0.6
    elif reason == "take_profit":
        signals.append(Signal("take", "익절 기준 도달", f"평균단가 대비 {pct(pnl)} (기준 +{take_pct:.1f}%) · 절반 매도", "sell", 0.7))
        qty_fraction = 0.5
        score = 0.62
    elif reason == "trend_break":
        signals.append(Signal("trend", "20일 이동평균 이탈", f"종가가 20일 평균 {row['ma20']:,.0f}원 아래로", "sell", 0.6))
        score = 0.58
    elif reason == "meanrev_exit":
        signals.append(Signal("mean", "평균 회귀 완료", f"20일 평균 {row['ma20']:,.0f}원까지 회복 · 평가손익 {pct(pnl)}", "sell", 0.65))
        score = 0.6
    elif reason == "time_exit":
        signals.append(Signal("time", "보유 기간 만료", _time_exit_detail(template, pnl), "sell", 0.55))
        score = 0.55
    else:  # rebalance
        signals.append(Signal("rebalance", "리밸런싱 제외", "이번 분기 가치 순위에서 빠졌습니다", "sell", 0.55))
        score = 0.55

    vc = row.get("vol_change5", float("nan"))
    if not math.isnan(vc):
        falling = row["ret1"] < 0
        if vc < 0:
            side = "sell" if falling else "neutral"
            detail = f"5일 평균 대비 {pct(vc, 0)}" + (", 하락 중 매수세 약화" if falling else "")
        else:
            side = "hold" if falling else "sell"
            detail = f"5일 평균 대비 {pct(vc, 0)}" + (", 저가 매수 유입 가능" if falling else ", 고점 매물 출회")
        signals.append(Signal("volume", "거래량 " + ("감소" if vc < 0 else "증가"), detail, side, clamp(abs(vc) / 0.6)))
    rsi = row.get("rsi14", float("nan"))
    if not math.isnan(rsi):
        if rsi < 30:
            signals.append(Signal("rsi", f"RSI(14) {rsi:.0f}", "과매도(30) 아래, 기술적 반등 가능", "hold", clamp((30 - rsi) / 20 + 0.3)))
        elif rsi > 70:
            signals.append(Signal("rsi", f"RSI(14) {rsi:.0f}", "과매수(70) 위, 되돌림 가능", "sell", clamp((rsi - 70) / 20 + 0.3)))
        else:
            signals.append(Signal("rsi", f"RSI(14) {rsi:.0f}", "중립 구간, 반등 신호 아직 없음" if pnl < 0 else "중립 구간", "sell" if pnl < 0 else "neutral", 0.3 if pnl < 0 else 0.1))
    signals.append(_market_signal(bench_ret1, float(row["ret1"]), "sell"))
    signals.append(_news_placeholder())
    metrics = _metrics(row) | {"pnl_pct": round(pnl * 100, 2), "avg_price": avg_price}
    return Candidate(symbol, name, "sell", reason, score, signals, qty_fraction, mandatory, metrics)


def _partial_note(row: pd.Series) -> str:
    fraction = row.get("vol_fraction", 1.0)
    return "" if fraction >= 1 else f" (장중 거래량을 하루치로 환산 · 지금까지 약 {fraction * 100:.0f}% 체결 가정)"


def _time_exit_detail(template: str | None, pnl: float) -> str:
    days, min_pnl = TIME_EXITS.get(template or "news", (NEWS_MAX_HOLD_DAYS, None))
    if min_pnl is None:
        return f"최대 보유 {days}거래일 도달 · 평가손익 {pct(pnl)}"
    return f"{days}거래일 보유했지만 수익 {pct(pnl)}로 기준 {pct(min_pnl)} 미만 · 흐름이 이어지지 않아 정리"


def _metrics(row: pd.Series) -> dict[str, Any]:
    keys = ["close", "ma20", "ma60", "ma250", "rsi14", "z20", "vol20", "vol_ratio", "ret1", "ret10", "ret60"]
    out: dict[str, Any] = {}
    for k in keys:
        v = row.get(k)
        if v is not None and not (isinstance(v, float) and math.isnan(v)):
            out[k] = round(float(v), 4 if k.startswith("ret") else 2)
    return out


# ---------------------------------------------------------------------------
# 가치 리밸런싱


def value_ranking(frames: dict[str, pd.DataFrame], fundamentals: dict[str, tuple[float | None, float | None]]) -> list[tuple[str, float, list[Signal]]]:
    """PER·PBR(키움 ka10001)이 있으면 그 순위를, 없으면 가격 대용 지표(250일 평균 대비 할인율·낮은 변동성)를 씁니다."""
    rows = []
    for sym, ind in frames.items():
        if len(ind) < 120:
            continue
        last = ind.iloc[-1]
        per, pbr = fundamentals.get(sym, (None, None))
        rows.append((sym, last, per, pbr))
    if not rows:
        return []
    have_fund = sum(1 for r in rows if r[3]) >= max(3, len(rows) // 2)
    scored = []
    for sym, last, per, pbr in rows:
        discount = float(last["close"] / last["ma250"] - 1) if last["ma250"] else 0.0
        vol = float(last["vol20"]) if not math.isnan(last["vol20"]) else 30.0
        signals = []
        if have_fund and pbr:
            score = -math.log(max(pbr, 0.05)) + (-math.log(max(per, 1)) * 0.5 if per and per > 0 else -1.0)
            signals.append(Signal("pbr", f"PBR {pbr:.2f}", "주가순자산비율 · 낮을수록 저평가", "buy", clamp(1.2 - pbr)))
            if per and per > 0:
                signals.append(Signal("per", f"PER {per:.1f}", "주가수익비율", "buy", clamp((20 - per) / 15)))
        else:
            score = -discount * 3 - vol / 40
            signals.append(Signal("discount", "250일 평균 대비", f"{pct(discount)} · 가격 대용 지표 (PER·PBR 없음)", "buy" if discount < 0 else "hold", clamp(-discount / 0.2)))
        signals.append(Signal("vol", "20일 변동성", f"연환산 {vol:.0f}%", "buy" if vol < 30 else "hold", clamp((40 - vol) / 30)))
        scored.append((sym, score, signals))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


# ---------------------------------------------------------------------------
# 과거 유사 신호 통계


def _reason_mask(reason: str, ind: pd.DataFrame, stop_pct: float, take_pct: float) -> tuple[pd.Series, str] | None:
    c = ind["close"]
    if reason == "breakout":
        return entry_frame("momentum", ind)["entry"], "buy"
    if reason == "meanrev_entry":
        return entry_frame("meanrev", ind)["entry"], "buy"
    if reason == "meanrev_exit":
        return (c >= ind["ma20"]) & (c.shift(1) < ind["ma20"].shift(1)), "sell"
    if reason == "trend_break":
        return (c < ind["ma20"]) & (c.shift(1) >= ind["ma20"].shift(1)), "sell"
    if reason == "stop_loss":
        return ind["ret10"] <= -stop_pct / 100, "sell"
    if reason == "take_profit":
        return ind["ret10"] >= take_pct / 100, "sell"
    return None


def signal_history(
    reason: str, frames: dict[str, pd.DataFrame], stop_pct: float, take_pct: float, horizon: int = 5, round_trip_cost: float = 0.0
) -> dict[str, Any] | None:
    """비슷한 신호 뒤 horizon 거래일 동안의 방향 적중률과 평균 수익률(%).

    매수 신호의 평균 수익률은 왕복 비용(수수료·세금·슬리피지)을 뺀 값이고,
    매도 신호는 팔지 않고 계속 보유했을 때의 평균 등락입니다.
    """
    total = hits = 0
    fwd_sum = 0.0
    side = "buy"
    for ind in frames.values():
        spec = _reason_mask(reason, ind, stop_pct, take_pct)
        if spec is None:
            return None
        mask, side = spec
        fwd = ind["fwd5"]
        valid = mask.fillna(False) & fwd.notna()
        n = int(valid.sum())
        if not n:
            continue
        total += n
        fwd_sum += float(fwd[valid].sum())
        hits += int(((fwd[valid] > 0) if side == "buy" else (fwd[valid] < 0)).sum())
    if total < 10:
        return None
    avg = fwd_sum / total - (round_trip_cost if side == "buy" else 0.0)
    return {"count": total, "hit_rate": round(hits / total * 100), "horizon": horizon, "side": side, "avg_return": round(avg * 100, 2)}
