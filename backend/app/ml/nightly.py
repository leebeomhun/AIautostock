"""야간 ML 배치: Alpha158 팩터 + LightGBM 으로 다음 거래일 종목 순위를 예측합니다.

qlib 의 기본 워크플로(Alpha158 → RobustZScoreNorm·Fillna → 라벨 CSRankNorm → LGBModel)를 같은 순서로 따릅니다.
실행: `uv run python -m app.ml.nightly`  (pm2 cron 으로 장 마감 후 평일마다)
결과: data/ml/latest.json 과 data/ml/<날짜>.json — 엔진이 근거 시그널 'ML 랭킹 (qlib)' 로 읽습니다.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from app.config import get_settings
from app.container import build_market
from app.db import now_kst
from app.market.base import krx_session_status
from app.market.universe import UNIVERSES
from app.ml.alpha158 import features, label

log = logging.getLogger("ml.nightly")

HISTORY_DAYS = 1000
VALID_DAYS = 60
MIN_TRAIN_DAYS = 250
MODEL_NAME = "Alpha158(qlib 정의, 157개) + LightGBM"
PARAMS = {
    "objective": "regression",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "max_depth": 6,
    "min_data_in_leaf": 40,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l1": 1.0,
    "lambda_l2": 10.0,
    "num_threads": 4,
    "verbose": -1,
    "seed": 7,
}


def universe(domestic_only: bool) -> list[str]:
    syms: list[str] = []
    for u in UNIVERSES.values():
        if domestic_only and not u["domestic"]:
            continue
        syms.extend(i.symbol for i in u["instruments"])
    return sorted(set(syms))


def build_panel(histories: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.Series]:
    xs, ys = [], []
    for sym, df in histories.items():
        f = features(df)
        f["symbol"] = sym
        xs.append(f.set_index("symbol", append=True))
        ys.append(label(df).to_frame("label").assign(symbol=sym).set_index("symbol", append=True)["label"])
    x = pd.concat(xs).sort_index()
    y = pd.concat(ys).reindex(x.index)
    x.index.names = y.index.names = ["date", "symbol"]
    return x, y


def robust_zscore(x: pd.DataFrame, fit: pd.DataFrame) -> pd.DataFrame:
    """qlib RobustZScoreNorm: 학습 구간의 중앙값·MAD 로 정규화하고 ±3 으로 자른 뒤 Fillna(0)."""
    median = fit.median()
    mad = (fit - median).abs().median() * 1.4826 + 1e-12
    return ((x - median) / mad).clip(-3, 3).fillna(0.0)


def cs_rank_norm(y: pd.Series) -> pd.Series:
    """qlib CSRankNorm: 날짜별 순위를 0 중심으로 펼칩니다."""
    return (y.groupby(level="date").rank(pct=True) - 0.5) * 3.46


def daily_ic(pred: pd.Series, y: pd.Series) -> tuple[float, float]:
    df = pd.DataFrame({"p": pred, "y": y}).dropna()
    by = df.groupby(level="date")
    ic = by.apply(lambda g: g["p"].corr(g["y"]) if len(g) > 2 else np.nan).dropna()
    ric = by.apply(lambda g: g["p"].rank().corr(g["y"].rank()) if len(g) > 2 else np.nan).dropna()
    return float(ic.mean()) if len(ic) else float("nan"), float(ric.mean()) if len(ric) else float("nan")


def train_and_score(histories: dict[str, pd.DataFrame]) -> dict[str, Any]:
    x, y = build_panel(histories)
    dates = x.index.get_level_values("date").unique().sort_values()
    labeled = y.dropna().index.get_level_values("date").unique().sort_values()
    if len(labeled) < MIN_TRAIN_DAYS + VALID_DAYS:
        raise RuntimeError(f"학습할 날짜가 부족합니다 ({len(labeled)}일)")
    valid_dates = labeled[-VALID_DAYS:]
    # 라벨이 이틀 뒤 가격을 쓰므로 학습과 검증 사이를 이틀 띄웁니다.
    train_dates = labeled[: -VALID_DAYS - 2]
    # 이동평균 창(60일)이 다 차기 전 구간은 버립니다.
    train_dates = train_dates[60:]
    in_dates = lambda ds: x.index.get_level_values("date").isin(ds)  # noqa: E731

    fit = x[in_dates(train_dates)]
    xn = robust_zscore(x, fit)
    yn = cs_rank_norm(y)
    tr, va = in_dates(train_dates), in_dates(valid_dates)
    tr_mask = tr & yn.notna().to_numpy()
    va_mask = va & yn.notna().to_numpy()
    dtrain = lgb.Dataset(xn[tr_mask], yn[tr_mask])
    dvalid = lgb.Dataset(xn[va_mask], yn[va_mask], reference=dtrain)
    model = lgb.train(PARAMS, dtrain, num_boost_round=500, valid_sets=[dvalid], callbacks=[lgb.early_stopping(50, verbose=False)])
    best = max(model.best_iteration or 0, 20)
    ic, rank_ic = daily_ic(pd.Series(model.predict(xn[va_mask], num_iteration=best), index=xn[va_mask].index), y[va_mask])

    # 검증까지 포함해 같은 반복 수로 다시 학습한 모델로 최신 날짜를 예측합니다.
    all_mask = (tr | va) & yn.notna().to_numpy()
    final = lgb.train(PARAMS, lgb.Dataset(xn[all_mask], yn[all_mask]), num_boost_round=best)
    latest = dates[-1]
    xl = xn[x.index.get_level_values("date") == latest]
    pred = pd.Series(final.predict(xl), index=xl.index.get_level_values("symbol")).sort_values(ascending=False)
    n = len(pred)
    scores = {
        sym: {"score": round(float(v), 5), "rank": i + 1, "pct": round(i / (n - 1), 4) if n > 1 else 0.0}
        for i, (sym, v) in enumerate(pred.items())
    }
    importance = pd.Series(final.feature_importance("gain"), index=xn.columns).sort_values(ascending=False)
    return {
        "as_of": latest.strftime("%Y-%m-%d"),
        "model": MODEL_NAME,
        "scores": scores,
        "metrics": {
            "valid_ic": round(ic, 4),
            "valid_rank_ic": round(rank_ic, 4),
            "train_days": int(len(train_dates)),
            "valid_days": int(len(valid_dates)),
            "symbols": n,
            "best_iteration": int(best),
            "top_features": [str(k) for k in importance.index[:8]],
        },
    }


def run(out_dir: Path) -> dict[str, Any]:
    settings = get_settings()
    market, _client = build_market(settings)
    symbols = universe(domestic_only=settings.use_kiwoom)
    histories: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            df = market.history(sym, HISTORY_DAYS)
        except Exception as exc:  # noqa: BLE001
            log.warning("시세 조회 실패 %s: %s", sym, exc)
            continue
        if len(df) >= MIN_TRAIN_DAYS + VALID_DAYS + 80:
            histories[sym] = df
    if len(histories) < 5:
        raise RuntimeError(f"시세가 있는 종목이 너무 적습니다 ({len(histories)}개)")
    started = time.time()
    result = train_and_score(histories)
    result["generated_at"] = now_kst().isoformat(timespec="seconds")
    result["data_source"] = market.name
    result["metrics"]["seconds"] = round(time.time() - started, 1)
    out_dir.mkdir(parents=True, exist_ok=True)
    body = json.dumps(result, ensure_ascii=False, indent=1)
    (out_dir / f"{result['as_of']}.json").write_text(body)
    tmp = out_dir / "latest.json.tmp"
    tmp.write_text(body)
    tmp.replace(out_dir / "latest.json")
    return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None, help="결과 폴더 (기본: DATA_DIR/ml)")
    parser.add_argument("--force", action="store_true", help="장중이어도 실행")
    args = parser.parse_args()
    # 장중에는 오늘 일봉이 확정되지 않아 학습·예측이 흔들립니다. pm2 가 처음 띄울 때도 여기서 걸러집니다.
    if not args.force and krx_session_status(now_kst()).open:
        log.info("정규장 중이라 건너뜁니다. 장 마감 후 다시 돌거나 --force 로 실행하세요")
        return
    out = args.out or get_settings().data_dir / "ml"
    started = datetime.now()
    result = run(out)
    m = result["metrics"]
    top = ", ".join(f"{s}({v['rank']})" for s, v in list(result["scores"].items())[:5])
    log.info("완료 %s · %d종목 · 검증 IC %.4f / Rank IC %.4f · 상위 %s · %s", result["as_of"], m["symbols"], m["valid_ic"], m["valid_rank_ic"], top, datetime.now() - started)


if __name__ == "__main__":
    main()
