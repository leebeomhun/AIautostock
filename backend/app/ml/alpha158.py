"""qlib Alpha158 팩터를 pandas 로 옮긴 것.

정의: microsoft/qlib (MIT) qlib/contrib/data/loader.py · Alpha158DL.get_feature_config, 라벨은 handler.py 의
`Ref($close, -2)/Ref($close, -1) - 1`. qlib 연산자와 같은 뜻으로 계산합니다.
- Slope/Rsquare/Resi: 창 안 시간축(0..d-1)에 대한 선형회귀
- IdxMax/IdxMin: 창 안 최댓값·최솟값 위치(1부터)
- Rank: 창 안에서 현재값의 백분위
키움 일봉에는 VWAP 이 없어 VWAP0 을 뺀 157개입니다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

WINDOWS = (5, 10, 20, 30, 60)
EPS = 1e-12


def _slope_r2_resi(y: pd.Series, d: int) -> tuple[pd.Series, pd.Series, pd.Series]:
    t = pd.Series(np.arange(len(y), dtype=float), index=y.index)
    cov = y.rolling(d).cov(t)
    var_t = t.rolling(d).var()
    slope = cov / var_t
    mean_y, mean_t = y.rolling(d).mean(), t.rolling(d).mean()
    intercept = mean_y - slope * mean_t
    resi = y - (intercept + slope * t)
    r2 = y.rolling(d).corr(t) ** 2
    return slope, r2, resi


def features(df: pd.DataFrame) -> pd.DataFrame:
    """df: open, high, low, close, volume (일봉, 날짜 오름차순) → Alpha158 팩터 DataFrame."""
    o, h, l, c, v = (df[k].astype(float) for k in ("open", "high", "low", "close", "volume"))
    f: dict[str, pd.Series] = {}

    # kbar
    f["KMID"] = (c - o) / o
    f["KLEN"] = (h - l) / o
    f["KMID2"] = (c - o) / (h - l + EPS)
    f["KUP"] = (h - np.maximum(o, c)) / o
    f["KUP2"] = (h - np.maximum(o, c)) / (h - l + EPS)
    f["KLOW"] = (np.minimum(o, c) - l) / o
    f["KLOW2"] = (np.minimum(o, c) - l) / (h - l + EPS)
    f["KSFT"] = (2 * c - h - l) / o
    f["KSFT2"] = (2 * c - h - l) / (h - l + EPS)
    # price (window 0)
    f["OPEN0"], f["HIGH0"], f["LOW0"] = o / c, h / c, l / c

    prev = c.shift(1)
    up = (c > prev).astype(float).where(prev.notna())
    down = (c < prev).astype(float).where(prev.notna())
    gain = (c - prev).clip(lower=0)
    loss = (prev - c).clip(lower=0)
    absd = (c - prev).abs()
    vprev = v.shift(1)
    vgain = (v - vprev).clip(lower=0)
    vloss = (vprev - v).clip(lower=0)
    vabs = (v - vprev).abs()
    logv = np.log(v + 1)
    chg = c / prev
    vchg = np.log(v / vprev.replace(0, np.nan) + 1)
    wv = (chg - 1).abs() * v

    for d in WINDOWS:
        r = lambda s: s.rolling(d)  # noqa: E731
        f[f"ROC{d}"] = c.shift(d) / c
        f[f"MA{d}"] = r(c).mean() / c
        f[f"STD{d}"] = r(c).std() / c
        slope, r2, resi = _slope_r2_resi(c, d)
        f[f"BETA{d}"] = slope / c
        f[f"RSQR{d}"] = r2
        f[f"RESI{d}"] = resi / c
        f[f"MAX{d}"] = r(h).max() / c
        f[f"MIN{d}"] = r(l).min() / c
        f[f"QTLU{d}"] = r(c).quantile(0.8) / c
        f[f"QTLD{d}"] = r(c).quantile(0.2) / c
        f[f"RANK{d}"] = r(c).rank(pct=True)
        f[f"RSV{d}"] = (c - r(l).min()) / (r(h).max() - r(l).min() + EPS)
        imax = r(h).apply(np.argmax, raw=True) + 1
        imin = r(l).apply(np.argmin, raw=True) + 1
        f[f"IMAX{d}"] = imax / d
        f[f"IMIN{d}"] = imin / d
        f[f"IMXD{d}"] = (imax - imin) / d
        f[f"CORR{d}"] = r(c).corr(logv)
        f[f"CORD{d}"] = r(chg).corr(vchg)
        f[f"CNTP{d}"] = r(up).mean()
        f[f"CNTN{d}"] = r(down).mean()
        f[f"CNTD{d}"] = r(up).mean() - r(down).mean()
        f[f"SUMP{d}"] = r(gain).sum() / (r(absd).sum() + EPS)
        f[f"SUMN{d}"] = r(loss).sum() / (r(absd).sum() + EPS)
        f[f"SUMD{d}"] = (r(gain).sum() - r(loss).sum()) / (r(absd).sum() + EPS)
        f[f"VMA{d}"] = r(v).mean() / (v + EPS)
        f[f"VSTD{d}"] = r(v).std() / (v + EPS)
        f[f"WVMA{d}"] = r(wv).std() / (r(wv).mean() + EPS)
        f[f"VSUMP{d}"] = r(vgain).sum() / (r(vabs).sum() + EPS)
        f[f"VSUMN{d}"] = r(vloss).sum() / (r(vabs).sum() + EPS)
        f[f"VSUMD{d}"] = (r(vgain).sum() - r(vloss).sum()) / (r(vabs).sum() + EPS)

    out = pd.DataFrame(f, index=df.index)
    return out.replace([np.inf, -np.inf], np.nan)


def label(df: pd.DataFrame) -> pd.Series:
    """qlib Alpha158 라벨: 다음 거래일 종가 대비 그다음 거래일 종가 수익률 (신호 다음 날 진입 가정)."""
    c = df["close"].astype(float)
    return c.shift(-2) / c.shift(-1) - 1
