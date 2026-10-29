"""야간 ML 배치: Alpha158 팩터 + LightGBM 으로 향후 5거래일 종목 순위를 예측합니다.

qlib 의 기본 워크플로(Alpha158 → RobustZScoreNorm·Fillna → 라벨 CSRankNorm → LGBModel)를 같은 순서로 따릅니다.
라벨은 전략 보유 기간에 맞춰 5거래일 수익률을 쓰고, 최근 구간을 여러 개로 나눠 각각 검증합니다.
키움에 연결돼 있으면 코스피 보통주 시가총액 상위 종목(ML_UNIVERSE_SIZE)까지 넓혀 학습합니다.
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
from app.insights import Insights
from app.market.base import krx_session_status
from app.market.calendar import is_trading_day
from app.market.flow_store import save_snapshot
from app.market.universe import UNIVERSES
from app.ml.alpha158 import features, label

log = logging.getLogger("ml.nightly")

HISTORY_DAYS = 1000
LABEL_HORIZON = 5  # 기본값. ML_LABEL_HORIZON 으로 바꿉니다
VALID_DAYS = 60
FOLDS = 3  # 최근 FOLDS × VALID_DAYS 거래일을 구간별로 따로 검증합니다
EARLY_STOP_DAYS = 40  # 반복 수는 학습 구간 끝에서 정해 검증 구간을 건드리지 않습니다
WARMUP_DAYS = 60  # 이동평균 창(60일)이 다 차기 전 구간은 버립니다
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


def universe(domestic_only: bool, market: Any = None, size: int = 0) -> list[str]:
    syms: list[str] = []
    for u in UNIVERSES.values():
        if domestic_only and not u["domestic"]:
            continue
        syms.extend(i.symbol for i in u["instruments"])
    top = getattr(market, "top_by_market_cap", None)
    if size and top is not None:
        try:
            syms.extend(top(size))
        except Exception as exc:  # noqa: BLE001
            log.warning("시가총액 상위 종목 조회 실패, 기본 종목만 학습합니다: %s", exc)
    return sorted(set(syms))


def build_panel(histories: dict[str, pd.DataFrame], horizon: int = LABEL_HORIZON) -> tuple[pd.DataFrame, pd.Series]:
    xs, ys = [], []
    for sym, df in histories.items():
        f = features(df)
        f["symbol"] = sym
        xs.append(f.set_index("symbol", append=True))
        ys.append(label(df, horizon).to_frame("label").assign(symbol=sym).set_index("symbol", append=True)["label"])
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


def _fit(xn: pd.DataFrame, yn: pd.Series, dates: pd.Index, in_dates: Any, gap: int) -> tuple[lgb.Booster, int]:
    """학습 구간 끝 EARLY_STOP_DAYS 일로 반복 수를 정한 모델."""
    inner_train, inner_valid = dates[: -EARLY_STOP_DAYS - gap], dates[-EARLY_STOP_DAYS:]
    tr = in_dates(inner_train) & yn.notna().to_numpy()
    va = in_dates(inner_valid) & yn.notna().to_numpy()
    dtrain = lgb.Dataset(xn[tr], yn[tr])
    dvalid = lgb.Dataset(xn[va], yn[va], reference=dtrain)
    model = lgb.train(PARAMS, dtrain, num_boost_round=500, valid_sets=[dvalid], callbacks=[lgb.early_stopping(50, verbose=False)])
    return model, max(model.best_iteration or 0, 20)


def train_and_score(histories: dict[str, pd.DataFrame], horizon: int = LABEL_HORIZON) -> dict[str, Any]:
    # 라벨이 보는 미래 구간만큼 학습과 검증 사이를 띄웁니다.
    gap = horizon + 1
    x, y = build_panel(histories, horizon)
    dates = x.index.get_level_values("date").unique().sort_values()
    labeled = y.dropna().index.get_level_values("date").unique().sort_values()[WARMUP_DAYS:]
    need = MIN_TRAIN_DAYS + EARLY_STOP_DAYS + FOLDS * (VALID_DAYS + gap)
    if len(labeled) < need:
        raise RuntimeError(f"학습할 날짜가 부족합니다 ({len(labeled)}일 / 필요 {need}일)")
    in_dates = lambda ds: x.index.get_level_values("date").isin(ds)  # noqa: E731
    yn = cs_rank_norm(y)

    # 구간마다 그 이전 데이터로만 정규화·학습하고 그 구간의 순위 IC 를 잽니다 (시간 순서를 지킨 교차 검증).
    folds: list[dict[str, Any]] = []
    for k in range(FOLDS, 0, -1):
        valid_dates = labeled[len(labeled) - k * VALID_DAYS : len(labeled) - (k - 1) * VALID_DAYS]
        train_dates = labeled[labeled < valid_dates[0]][:-gap]
        xn = robust_zscore(x, x[in_dates(train_dates)])
        model, best = _fit(xn, yn, train_dates, in_dates, gap)
        va = in_dates(valid_dates) & yn.notna().to_numpy()
        ic, rank_ic = daily_ic(pd.Series(model.predict(xn[va], num_iteration=best), index=xn[va].index), y[va])
        folds.append({"start": valid_dates[0].strftime("%Y-%m-%d"), "end": valid_dates[-1].strftime("%Y-%m-%d"), "ic": round(ic, 4), "rank_ic": round(rank_ic, 4), "best_iteration": int(best)})

    # 라벨이 있는 전체 구간으로 다시 학습한 모델로 최신 날짜를 예측합니다.
    best = int(np.median([f["best_iteration"] for f in folds]))
    xn = robust_zscore(x, x[in_dates(labeled)])
    all_mask = in_dates(labeled) & yn.notna().to_numpy()
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
    fold_ric = [f["rank_ic"] for f in folds]
    return {
        "as_of": latest.strftime("%Y-%m-%d"),
        "model": f"{MODEL_NAME} · {horizon}일 라벨",
        "scores": scores,
        "metrics": {
            "label_horizon": horizon,
            "valid_ic": round(float(np.nanmean([f["ic"] for f in folds])), 4),
            "valid_rank_ic": round(float(np.nanmean(fold_ric)), 4),
            "fold_rank_ics": fold_ric,
            "positive_folds": int(sum(1 for v in fold_ric if v > 0)),
            "folds": folds,
            "train_days": int(len(labeled) - VALID_DAYS - gap),
            "valid_days": int(FOLDS * VALID_DAYS),
            "symbols": n,
            "best_iteration": best,
            "top_features": [str(k) for k in importance.index[:8]],
        },
    }


def snapshot_flow(market: Any, data_dir: Path) -> int:
    """장 마감 뒤 기관·외국인 연속 순매수 목록(ka10131)을 하루 한 번 남깁니다 (수급 가산 검증용). 저장한 종목 수."""
    get = getattr(market, "investor_flow", None)
    if get is None:
        return 0
    try:
        flow = get()
        if flow:
            save_snapshot(data_dir, flow, now_kst(), "eod")
        return len(flow)
    except Exception as exc:  # noqa: BLE001 — 수급 저장이 실패해도 학습은 계속
        log.warning("수급 스냅샷 저장 실패: %s", exc)
        return 0


def run(out_dir: Path) -> dict[str, Any]:
    settings = get_settings()
    market, _client = build_market(settings)
    log.info("수급 스냅샷 %d종목 저장", snapshot_flow(market, settings.data_dir))
    symbols = universe(domestic_only=settings.use_kiwoom, market=market, size=settings.ml_universe_size)
    histories: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            df = market.history(sym, HISTORY_DAYS)
        except Exception as exc:  # noqa: BLE001
            log.warning("시세 조회 실패 %s: %s", sym, exc)
            continue
        if len(df) >= MIN_TRAIN_DAYS + 80:
            histories[sym] = df
    if len(histories) < 5:
        raise RuntimeError(f"시세가 있는 종목이 너무 적습니다 ({len(histories)}개)")
    started = time.time()
    result = train_and_score(histories, settings.ml_label_horizon)
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
    if not args.force and not is_trading_day(now_kst().date()):
        log.info("KRX 휴장일이라 새 일봉이 없어 건너뜁니다. --force 로 실행할 수 있습니다")
        return
    out = args.out or get_settings().data_dir / "ml"
    started = datetime.now()
    result = run(out)
    m = result["metrics"]
    top = ", ".join(f"{s}({v['rank']})" for s, v in list(result["scores"].items())[:5])
    log.info(
        "완료 %s · %d종목 · 검증 IC %.4f / Rank IC %.4f (구간별 %s) · 상위 %s · %s",
        result["as_of"], m["symbols"], m["valid_ic"], m["valid_rank_ic"], m["fold_rank_ics"], top, datetime.now() - started,
    )
    # 운영 점검: 엔진이 이 결과를 판단 점수에 반영하는지 (app.insights.Insights.ml_gate 와 같은 기준)
    ok, why = Insights(get_settings().data_dir).ml_gate(m["valid_rank_ic"], m["fold_rank_ics"])
    log.info("판단 반영: %s · %s", "예" if ok else "아니오 (근거로만 표시)", why)


if __name__ == "__main__":
    main()
