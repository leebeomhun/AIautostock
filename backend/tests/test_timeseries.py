import math

import numpy as np
import pandas as pd
import pytest

from app.quant import timeseries as ts


def series(values) -> pd.Series:
    return pd.Series(values, index=pd.bdate_range("2026-01-01", periods=len(values)), dtype=float)


def test_rsi_matches_wilder_definition():
    x = series([44, 44.3, 44.1, 43.6, 44.3, 44.8, 45.1, 45.4, 45.8, 46.1, 45.9, 46.3, 46.8, 46.5, 46.2, 46.6, 46.0])
    rsi = ts.relative_strength_index(x, 14)

    # gs-quant 와 같은 정의를 직접 풀어서 계산합니다.
    change = x.diff().iloc[1:]
    gains, losses = change.clip(lower=0), (-change).clip(lower=0)
    n = 14

    def wilder(v: pd.Series) -> list[float]:
        out = [v.rolling(n, 0).mean().iloc[n]]
        for val in v.iloc[n + 1 :]:
            out.append(((n - 1) * out[-1] + val) / n)
        return out

    g, l = wilder(gains), wilder(losses)
    expected = [100 - 100 / (1 + a / b) for a, b in zip(g, l)]
    assert list(rsi.round(8)) == pytest.approx([round(e, 8) for e in expected])
    assert 0 < rsi.iloc[-1] < 100


def test_rsi_is_100_without_losses():
    x = series(np.arange(1, 40))
    assert (ts.relative_strength_index(x, 14) == 100).all()


def test_volatility_is_annualized_percent():
    rng = np.random.default_rng(0)
    r = rng.normal(0, 0.01, 2000)
    x = series(100 * np.exp(np.cumsum(r)))
    vol = ts.volatility(x, ts.Window(len(x), 0)).iloc[-1]
    assert vol == pytest.approx(0.01 * math.sqrt(252) * 100, rel=0.08)


def test_max_drawdown_and_annualized_return():
    x = series([100, 120, 90, 110, 130])
    assert ts.max_drawdown(x).iloc[-1] == pytest.approx(90 / 120 - 1)
    flat = pd.Series([100.0, 110.0], index=pd.to_datetime(["2025-01-01", "2026-01-01"]))
    assert ts.annualized_return(flat) == pytest.approx(0.1, rel=1e-3)


def test_zscores_rolling_last_value():
    x = series([1, 2, 3, 4, 10])
    z = ts.zscores(x, ts.Window(5, 0)).iloc[-1]
    w = np.array([1, 2, 3, 4, 10.0])
    assert z == pytest.approx((10 - w.mean()) / w.std(ddof=1))


def test_bollinger_bands_contain_mean():
    x = series(np.linspace(1, 30, 30))
    bb = ts.bollinger_bands(x, ts.Window(20, 0), 2)
    ma = ts.moving_average(x, ts.Window(20, 0))
    assert (bb["lower"].iloc[5:] <= ma.iloc[5:]).all() and (bb["upper"].iloc[5:] >= ma.iloc[5:]).all()
