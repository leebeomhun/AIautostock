"""ML 예측 순위 IC 를 같은 날 횡단면 랜덤 셔플(null)과 짝지어 비교합니다.

순위 IC 를 0 과만 비교하면 시장·규모처럼 종목 전체가 함께 움직이는 요인 덕에 생긴 IC 도 통과합니다.
같은 날 예측값을 종목끼리 섞은 대조군 IC 를 날짜별로 빼고(paired alpha) 그 평균의 t 값을 봅니다.
라벨이 h 거래일 수익률이면 겹치는 창 때문에 t 가 √h 배쯤 부풀므로 마지막 날부터 h 일 간격으로만 씁니다.

방식 출처: Vibe-Trading (https://github.com/HKUDS/Vibe-Trading) agent/src/factors/bench_runner_strict.py
(_shuffle_within_rows · compute_random_ic_series · alpha_series_paired · t_stat) 와
agent/src/tools/alpha_bench_tool.py (_compute_forward_returns 의 겹치지 않는 표본). 코드를 옮겨 다시 썼습니다.
Copyright (c) 2026 Vibe-Trading Contributors. MIT License.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

SEEDS = 5
MIN_NAMES = 5  # 그날 예측·라벨이 다 있는 종목이 이보다 적으면 그날은 뺍니다


def _rank_corr(a: np.ndarray, b: np.ndarray) -> float:
    if a.std() == 0 or b.std() == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def random_null(pred: pd.Series, y: pd.Series, horizon: int = 1, seeds: int = SEEDS, base_seed: int = 42) -> dict[str, Any]:
    """pred·y 는 (date, symbol) 인덱스. alpha_t 는 정의할 수 없으면 None (표본 2개 미만, 분산 0)."""
    df = pd.DataFrame({"p": pred, "y": y}).dropna()
    dates = df.index.get_level_values("date").unique().sort_values()
    keep = set(dates[::-1][:: max(1, int(horizon))])
    rng = np.random.default_rng(base_seed)
    real, null = [], []
    for day, g in df.groupby(level="date"):
        if day not in keep or len(g) < MIN_NAMES:
            continue
        p, r = g["p"].rank().to_numpy(), g["y"].rank().to_numpy()
        ic = _rank_corr(p, r)
        shuffled = [_rank_corr(rng.permutation(p), r) for _ in range(seeds)]
        if math.isnan(ic) or any(math.isnan(v) for v in shuffled):
            continue
        real.append(ic)
        null.append(float(np.mean(shuffled)))
    alpha = np.array(real) - np.array(null)
    n = len(alpha)
    std = float(alpha.std(ddof=1)) if n >= 2 else 0.0
    t = float(alpha.mean() / (std / math.sqrt(n))) if n >= 2 and std > 0 else None
    return {
        "seeds": seeds,
        "dates": n,
        "rank_ic": round(float(np.mean(real)), 4) if n else None,
        "null_rank_ic": round(float(np.mean(null)), 4) if n else None,
        "alpha_t": round(t, 2) if t is not None else None,
    }
