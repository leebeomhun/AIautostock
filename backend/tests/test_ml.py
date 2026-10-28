import numpy as np
import pandas as pd

from app.market.simulated import SimulatedMarket
from app.market.universe import KOSPI200
from app.ml.alpha158 import features, label
from app.ml.nightly import train_and_score


def test_alpha158_definitions():
    idx = pd.bdate_range("2025-01-01", periods=80)
    close = pd.Series(np.linspace(100, 179, 80), index=idx)
    df = pd.DataFrame({"open": close - 1, "high": close + 2, "low": close - 2, "close": close, "volume": 1000.0}, index=idx)
    f = features(df)
    assert f.shape[1] == 157
    last = f.iloc[-1]
    # 직선으로 오르는 가격: 결정계수 1, 잔차 0, 최근값이 창 안 최고 순위, 매일 상승
    assert abs(last["RSQR20"] - 1) < 1e-9 and abs(last["RESI20"]) < 1e-9
    assert last["RANK10"] == 1.0 and last["CNTP20"] == 1.0 and last["SUMP20"] > 0.99
    assert abs(last["BETA5"] - 1 / 179) < 1e-9  # 하루 1씩 오르는 기울기 / 종가
    assert abs(last["ROC5"] - 174 / 179) < 1e-12
    lab = label(df)
    assert abs(lab.iloc[0] - (102 / 101 - 1)) < 1e-12 and lab.iloc[-2:].isna().all()


def test_train_and_score_on_simulated_market():
    m = SimulatedMarket(seed=3)
    hist = {i.symbol: m.history(i.symbol, 700) for i in KOSPI200[:12]}
    out = train_and_score(hist)
    assert out["metrics"]["symbols"] == 12
    ranks = sorted(v["rank"] for v in out["scores"].values())
    assert ranks == list(range(1, 13))
    best = min(out["scores"].values(), key=lambda v: v["rank"])
    assert best["pct"] == 0.0
