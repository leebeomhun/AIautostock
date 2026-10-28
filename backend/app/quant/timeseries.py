"""gs_quant.timeseries 의 지표를 같은 정의로 옮긴 함수들.

gs-quant(https://github.com/goldmansachs/gs-quant)는 대부분의 기능이 Marquee 데이터 접근을 전제로 하므로
의존성으로 넣지 않고, 로컬 pandas 시리즈에서 동작하는 시계열 함수만 같은 이름·같은 수식으로 구현했습니다.

- Window(w, r): 롤링 창 크기 w, 앞부분 r개 관측치를 버리는 ramp (gs_quant.timeseries.helper.Window)
- volatility: 수익률 표준편차를 연환산하고 ×100 (퍼센트) — gs_quant.timeseries.econometrics.volatility
- relative_strength_index: Wilder 평활 이동평균 기반 RSI — gs_quant.timeseries.technicals
- max_drawdown: 롤링 고점 대비 하락률의 롤링 최솟값 — gs_quant.timeseries.econometrics.max_drawdown
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

ANNUALIZATION_DAILY = 252


@dataclass(frozen=True)
class Window:
    w: int | None = None
    r: int = 0


def normalize_window(x: pd.Series, w: Window | int | None) -> Window:
    if w is None:
        return Window(len(x), 0)
    if isinstance(w, int):
        return Window(w, w)
    return Window(w.w if w.w is not None else len(x), w.r)


def apply_ramp(x: pd.Series, w: Window) -> pd.Series:
    return x.iloc[w.r :] if w.r else x


def returns(series: pd.Series, obs: int = 1, type: str = "simple") -> pd.Series:  # noqa: A002
    if series.size < 1:
        return series
    if type == "logarithmic":
        return np.log(series) - np.log(series.shift(obs))
    return series / series.shift(obs) - 1


def index(x: pd.Series, initial: float = 1) -> pd.Series:
    """첫 유효값을 initial 로 맞춘 지수 (gs_quant.timeseries.econometrics.index)."""
    first = x.dropna()
    if first.empty:
        return x
    return x / first.iloc[0] * initial


def mean(x: pd.Series, w: Window | int | None = None) -> pd.Series:
    w = normalize_window(x, w)
    return apply_ramp(x.rolling(w.w, 0).mean(), w)


def std(x: pd.Series, w: Window | int | None = None) -> pd.Series:
    w = normalize_window(x, w)
    return apply_ramp(x.rolling(w.w, 0).std(), w)


def annualize(x: pd.Series, factor: int = ANNUALIZATION_DAILY) -> pd.Series:
    return x * math.sqrt(factor)


def volatility(x: pd.Series, w: Window | int | None = None, returns_type: str = "simple") -> pd.Series:
    """연환산 변동성(%). gs-quant 와 같이 수익률의 롤링 표준편차 × √252 × 100."""
    w = normalize_window(x, w)
    if x.size < 1:
        return x
    ret = returns(x, type=returns_type)
    vol = ret.rolling(w.w, 0).std()
    return apply_ramp(annualize(vol) * 100, w)


def moving_average(x: pd.Series, w: Window | int | None = None) -> pd.Series:
    w = normalize_window(x, w)
    return apply_ramp(x.rolling(w.w, 0).mean(), w)


def bollinger_bands(x: pd.Series, w: Window | int | None = None, k: float = 2) -> pd.DataFrame:
    w = normalize_window(x, w)
    avg = x.rolling(w.w, 0).mean()
    sd = x.rolling(w.w, 0).std()
    bands = pd.DataFrame({"lower": avg - k * sd, "upper": avg + k * sd})
    return bands.iloc[w.r :] if w.r else bands


def smoothed_moving_average(x: pd.Series, w: Window | int | None = None) -> pd.Series:
    """Wilder 평활: S_t = ((N-1)·S_{t-1} + X_t) / N, 시작값은 첫 창의 단순평균."""
    w = normalize_window(x, w)
    n = int(w.w)
    means = apply_ramp(x.rolling(n, 0).mean(), w)
    if means.size < 1:
        return pd.Series(dtype=float)
    xs = apply_ramp(x, w)
    values = xs.to_numpy(dtype=float)
    out = np.empty_like(values)
    out[0] = means.iloc[0]
    for i in range(1, len(values)):
        out[i] = ((n - 1) * out[i - 1] + values[i]) / n
    return pd.Series(out, index=xs.index)


def relative_strength_index(x: pd.Series, w: Window | int = 14) -> pd.Series:
    w = normalize_window(x, w)
    change = x.diff(1).iloc[1:]
    gains = change.clip(lower=0)
    losses = (-change).clip(lower=0)
    avg_gain = smoothed_moving_average(gains, w)
    avg_loss = smoothed_moving_average(losses, w)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        rsi = 100 - 100 / (1 + rs)
    return rsi.where(avg_loss != 0, 100.0)


def exponential_moving_average(x: pd.Series, beta: float = 0.75) -> pd.Series:
    return x.ewm(alpha=1 - beta, adjust=False).mean()


def macd(x: pd.Series, m: int = 12, n: int = 26, s: int = 1) -> pd.Series:
    a = x.ewm(adjust=False, span=m).mean()
    b = x.ewm(adjust=False, span=n).mean()
    return (a - b).ewm(adjust=False, span=s).mean()


def zscores(x: pd.Series, w: Window | int | None = None) -> pd.Series:
    """롤링 z-score (ddof=1). 창 안 마지막 값의 z-score — gs_quant.timeseries.statistics.zscores."""
    if w is None:
        clean = x.dropna()
        if clean.size <= 1:
            return pd.Series(0.0, index=x.index)
        return ((x - clean.mean()) / clean.std(ddof=1)).astype(float)
    w = normalize_window(x, w)
    roll = x.rolling(w.w, 0)
    z = (x - roll.mean()) / roll.std(ddof=1)
    return apply_ramp(z.fillna(0.0), w)


def max_drawdown(x: pd.Series, w: Window | int | None = None) -> pd.Series:
    w = normalize_window(x, w)
    rolling_max = x.rolling(w.w, 0).max()
    result = (x / rolling_max - 1).rolling(w.w, 0).min()
    return apply_ramp(result, w)


def annualized_return(levels: pd.Series) -> float:
    """전체 구간 연환산 수익률(CAGR), 365.25일 기준 — gs-quant _annualized_return 과 같은 기준."""
    clean = levels.dropna()
    if clean.size < 2:
        return 0.0
    days = (clean.index[-1] - clean.index[0]).days
    if days <= 0 or clean.iloc[0] <= 0:
        return 0.0
    return float((clean.iloc[-1] / clean.iloc[0]) ** (365.25 / days) - 1)


def sharpe_ratio(levels: pd.Series, risk_free_rate: float = 0.0) -> float:
    """연환산 초과수익 / 연환산 변동성."""
    clean = levels.dropna()
    if clean.size < 3:
        return 0.0
    ret = returns(clean).dropna()
    vol = float(ret.std() * math.sqrt(ANNUALIZATION_DAILY))
    if vol == 0:
        return 0.0
    return (annualized_return(clean) - risk_free_rate) / vol
