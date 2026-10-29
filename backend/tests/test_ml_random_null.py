"""ML 판단 기준의 랜덤 셔플 대조군 (app.ml.random_null, Insights.ml_gate)."""

import json

import numpy as np
import pandas as pd

from app.api.routes import StrategyIn, create_strategy
from app.db import now_kst
from app.insights import Insights
from app.market.simulated import SimulatedMarket
from app.market.universe import KOSPI200
from app.ml.nightly import train_and_score
from app.ml.random_null import random_null
from app.ops import ml_review


def _panel(signal: float, days: int = 200, names: int = 30, seed: int = 1) -> tuple[pd.Series, pd.Series]:
    rng = np.random.default_rng(seed)
    idx = pd.MultiIndex.from_product([pd.bdate_range("2026-01-01", periods=days), [f"S{i:02d}" for i in range(names)]], names=["date", "symbol"])
    pred = pd.Series(rng.normal(size=len(idx)), index=idx)
    y = signal * pred + pd.Series(rng.normal(size=len(idx)), index=idx)
    return pred, y


def test_random_null_separates_real_signal_from_noise():
    real = random_null(*_panel(0.3), horizon=1)
    noise = random_null(*_panel(0.0), horizon=1)
    assert real["seeds"] == 5 and real["dates"] == 200
    assert real["alpha_t"] > 5 and real["rank_ic"] > 0.1 and abs(real["null_rank_ic"]) < 0.05
    assert abs(noise["alpha_t"]) < 3


def test_random_null_samples_non_overlapping_dates_for_multi_day_labels():
    assert random_null(*_panel(0.3), horizon=5)["dates"] == 40  # 200일 중 마지막 날부터 5일 간격
    pred, y = _panel(0.3, days=1)
    assert random_null(pred, y)["alpha_t"] is None  # 날짜 하나로는 t 를 정의할 수 없습니다
    flat = pd.Series(1.0, index=pred.index)
    assert random_null(flat, y)["dates"] == 0 and random_null(flat, y)["rank_ic"] is None  # 예측이 모두 같으면 IC 없음


def _metrics(null_t):
    m = {"valid_rank_ic": 0.05, "fold_rank_ics": [0.05, 0.04, 0.06], "label_horizon": 5}
    return m if null_t == "missing" else m | {"random_null": {"seeds": 5, "dates": 36, "alpha_t": null_t}}


def test_ml_gate_requires_clear_margin_over_random_null(tmp_path):
    ins = Insights(tmp_path)
    ok, why = ins.ml_gate(_metrics(3.2))
    assert ok and "랜덤 대조 t 3.2" in why
    for bad, text in ((2.0, "기준 2 초과"), (-1.0, "t -1.0"), (None, "결과가 없어"), ("missing", "결과가 없어")):
        ok, why = ins.ml_gate(_metrics(bad))
        assert not ok and text in why and "참고만" in why
    assert not ins.ml_gate(None)[0]
    assert Insights(tmp_path, min_null_t=3.5).ml_gate(_metrics(3.2))[0] is False


def _write(tmp_path, metrics, scores):
    (tmp_path / "ml").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ml" / "latest.json").write_text(json.dumps({"as_of": now_kst().strftime("%Y-%m-%d"), "model": "m", "metrics": metrics, "scores": scores}))


def test_ml_boost_and_ops_follow_random_null(tmp_path):
    scores = {"A": {"score": 1, "rank": 1, "pct": 0.1}}
    _write(tmp_path, _metrics(1.2), scores)
    sig, adjust = Insights(tmp_path).ml_signal("A", "buy", now_kst().date())
    assert adjust == 0.0 and sig.side == "neutral" and "랜덤 셔플 대조" in sig.detail
    review = ml_review(Insights(tmp_path), now_kst().date())
    assert not review["ok"] and review["random_null"]["alpha_t"] == 1.2
    _write(tmp_path, _metrics(2.6), scores)
    assert Insights(tmp_path).ml_signal("A", "buy", now_kst().date())[1] > 0
    assert ml_review(Insights(tmp_path), now_kst().date())["ok"]


def test_ml_topn_holds_off_when_random_null_fails(container):
    c = container
    c.settings.enable_ml_topn_strategy = True
    _write(c.settings.data_dir, _metrics(1.0), {"005930": {"score": 1, "rank": 1, "pct": 0.0}, "000660": {"score": 0, "rank": 2, "pct": 1.0}})
    create_strategy(c, StrategyIn(template="ml_topn", universe="custom", custom_symbols=["005930", "000660"], amount=5_000_000, stop_pct=5, take_pct=10, max_weight_pct=15))
    picks, why = c.engine._ml_topn_picks(["005930", "000660"], now_kst())  # noqa: SLF001
    assert picks is None and "검증 미달" in why and "랜덤 셔플 대조" in why


def test_nightly_metrics_include_random_null():
    m = SimulatedMarket(seed=3)
    out = train_and_score({i.symbol: m.history(i.symbol, 700) for i in KOSPI200[:12]})
    null = out["metrics"]["random_null"]
    assert null["seeds"] == 5 and null["dates"] == 36  # 검증 3구간 × 60일을 5일 간격으로
    assert null["alpha_t"] is None or isinstance(null["alpha_t"], float)
