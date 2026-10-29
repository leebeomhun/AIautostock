"""모멘텀 재설계 진단 백테스트: 예전 규칙(baseline) vs 새 규칙 vs KOSPI vs 동일가중 보유(EW).

주 검증 구간은 2018–2022 (모멘텀 파라미터를 정한 2023–26 표본 밖)입니다. 2023-01-01 이후는 봉인 홀드아웃이라 참고로만 싣고,
이 구간 결과로 파라미터를 고르지 않습니다 (2023-10~2026-10 은 예전 파라미터를 정한 3년 표본과 겹쳐 진짜 표본 밖도 아닙니다).

규칙 — 진입·청산 파라미터(52주 20%, 거래량 1.4배, 트레일링 ×4, 10일 +3% 등)는 둘 다 운영 기본값 그대로입니다.
- baseline (2026-10-29 이전 운영): 약세장(KOSPI < 60일선) 모멘텀 매수 금액 50%.
  계좌 = 모멘텀 70% + 평균회귀 30%, 약세장에는 모멘텀 몫의 30% 를 평균회귀로 옮김.
- 새 규칙: 약세장 모멘텀 신규 진입 0 (보유 종목은 청산 규칙대로). 계좌 = 모멘텀 70% + 현금 30% (평균회귀 일시정지).
비용: 수수료 0.015%, 슬리피지 0.05%, 매도세 0.20%, 상한가 종가 매수 미체결 (운영 백테스트 기본값).
종목 범위: research/universe_candidates.json 후보 중 구간 시작 전 해의 평균 거래대금 상위 40 + 기본 22 (그 전 해 상반기 이전 상장 종목만).
후보가 지금 시총 상위라 생존 편향이 남아 절대 수익은 낙관적입니다 → 같은 범위의 동일가중 보유와 나란히 봅니다.
연도별 행은 그해 1월 1일에 현금으로 새로 시작한 결과이고, 누적 행은 구간 전체를 이어서 돌린 결과입니다.

데이터: research/fetch_long_data.py (pykrx 네이버 수정주가 + yfinance ^KS11)
실행: backend/.venv/bin/python research/long_check.py [--data research/data/long] [--out research/results]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "backend"))

import app.quant.allocation as allocation  # noqa: E402
import app.quant.backtest as bt  # noqa: E402
from app.quant.backtest import BacktestParams, BacktestResult, Sleeve, run_backtest, run_portfolio_backtest  # noqa: E402
from app.quant.signals import indicators, weak_market  # noqa: E402

MAIN_YEARS = range(2018, 2023)
HOLDOUT_START = 2023
TOP_N = 40
MOMENTUM = {"template": "momentum", "amount": 10_000_000, "stop_pct": 5, "take_pct": 10, "max_weight_pct": 15}
COLUMNS = {
    "kospi": "KOSPI",
    "ew": "EW 보유",
    "baseline_mom": "모멘텀 (예전: 약세 50%)",
    "new_mom": "모멘텀 (새: 약세 0)",
    "baseline_acct": "계좌 예전 (모멘텀70+평균회귀30, 이동 0.3)",
    "new_acct": "계좌 새 (모멘텀70+현금30)",
}


def _legacy_regime_allocations(items: Any, weak: bool, shift: float) -> dict[Any, float]:
    """2026-10-29 이전 app.quant.allocation.regime_allocations (커밋 e9aa639): 약세장 모멘텀 몫 shift 를 평균회귀로 옮김."""
    rows = list(items)
    out = {key: float(amount) for key, _template, amount in rows}
    shift = min(max(shift, 0.0), 1.0)
    receivers = [(key, float(amount)) for key, template, amount in rows if template == "meanrev" and amount > 0]
    if not weak or shift <= 0 or not receivers:
        return out
    moved = 0.0
    for key, template, amount in rows:
        if template == "momentum":
            out[key] = float(amount) * (1 - shift)
            moved += float(amount) * shift
    total = sum(amount for _key, amount in receivers)
    for key, amount in receivers:
        out[key] = amount + moved * amount / total
    return out


@contextmanager
def legacy_rules() -> Iterator[None]:
    """baseline 재현: 백테스트 모듈의 약세장 매수 배수와 국면 배분 함수를 잠시 예전 것으로 바꿉니다."""
    size, alloc = bt.WEAK_MARKET_SIZE, allocation.regime_allocations
    bt.WEAK_MARKET_SIZE, allocation.regime_allocations = {"momentum": 0.5}, _legacy_regime_allocations
    try:
        yield
    finally:
        bt.WEAK_MARKET_SIZE, allocation.regime_allocations = size, alloc


def load(data: Path) -> tuple[dict[str, pd.DataFrame], pd.Series, dict[str, Any]]:
    raw = {}
    for f in sorted((data / "cache").glob("*.json")):
        p = json.loads(f.read_text())
        raw[f.stem] = pd.DataFrame({k: p[k] for k in ("open", "high", "low", "close", "volume")}, index=pd.to_datetime(p["dates"]))
    b = json.loads((data / "bench.json").read_text())
    bench = pd.Series({pd.Timestamp(k): v for k, v in b["close"].items()}).sort_index()
    return raw, bench, b


def pick_universe(raw: dict[str, pd.DataFrame], year_before: int, core: list[str], listed_by: str | None = None) -> list[str]:
    """listed_by(기본: 그 전 해 6월 말) 이전부터 시세가 있는 종목 중 그 전 해 평균 거래대금 상위 TOP_N + 기본 종목."""
    listed = {s: df for s, df in raw.items() if df.index[0] <= pd.Timestamp(listed_by or f"{year_before}-06-30")}
    value = {}
    for s, df in listed.items():
        y = df.loc[str(year_before)]
        if len(y) > 200:
            value[s] = float((y["close"] * y["volume"]).mean())
    top = sorted(value, key=value.get, reverse=True)[:TOP_N]
    return sorted(set(top) | {s for s in core if s in listed})


def _span(bench: pd.Series, start: str, end: str) -> tuple[pd.Series, float]:
    """구간 지수와, run_backtest 의 years 로 넘길 값 (구간 마지막 날에서 시작일까지 정확히 거슬러 가도록)."""
    b = bench[bench.index <= pd.Timestamp(end)]
    days = (b.index[-1] - pd.Timestamp(start)).days
    return b, (days + 0.5) / 365


def _mdd(x: pd.Series) -> float:
    return float((x / x.cummax() - 1).min() * 100)


def _passive(frames: dict[str, pd.DataFrame], bench: pd.Series, start: str, end: str) -> dict[str, dict[str, float]]:
    k = bench.loc[start:end]
    closes = pd.DataFrame({s: f["close"].loc[start:end] for s, f in frames.items()}).ffill()
    closes = closes.loc[:, closes.iloc[0].notna()]  # 구간 시작에 시세가 있는 종목만
    ew = (closes / closes.iloc[0]).mean(axis=1)
    return {
        "kospi": {"ret": round(float(k.iloc[-1] / k.iloc[0] - 1) * 100, 1), "mdd": round(_mdd(k), 1)},
        "ew": {"ret": round(float(ew.iloc[-1] - 1) * 100, 1), "mdd": round(_mdd(ew), 1), "symbols": int(closes.shape[1])},
    }


def _row(r: BacktestResult) -> dict[str, Any]:
    return {"ret": r.total_return, "mdd": r.mdd, "cagr": r.cagr, "sharpe": r.sharpe, "trades": r.trades, "orders": r.orders, "warnings": r.warnings}


def run_window(frames: dict[str, pd.DataFrame], bench: pd.Series, start: str, end: str) -> dict[str, Any]:
    fr = {s: f[f.index <= pd.Timestamp(end)] for s, f in frames.items()}
    b, years = _span(bench, start, end)
    acct_old = [Sleeve("momentum", 0.7, 5, 10, 15), Sleeve("meanrev", 0.3, 5, 10, 15)]
    with legacy_rules():
        base_mom = run_backtest(BacktestParams(**MOMENTUM, years=years), fr, b)
        base_acct = run_portfolio_backtest(acct_old, fr, b, regime_shift=0.3, years=years)
    new_mom = run_backtest(BacktestParams(**MOMENTUM, years=years), fr, b)
    new_acct = run_portfolio_backtest([Sleeve("momentum", 0.7, 5, 10, 15)], fr, b, regime_shift=0.0, years=years)
    weak = weak_market(bench).loc[start:end]
    return {
        "start": base_mom.start,
        "end": base_mom.end,
        "weak_days_pct": round(float(weak.mean()) * 100),
        **_passive(fr, bench, start, end),
        "baseline_mom": _row(base_mom),
        "new_mom": _row(new_mom),
        "baseline_acct": _row(base_acct),
        "new_acct": _row(new_acct),
    }


def _cell(v: Any, digits: int = 1) -> str:
    return "—" if v is None else f"{v:+.{digits}f}%"


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 모멘텀 재설계 진단 백테스트",
        "",
        f"생성 {report['generated']} · 데이터 {report['data_range']} · 지수 {report['bench_source']} · 스크립트 research/long_check.py",
        "",
        "규칙과 한계는 스크립트 머리말 참고. 진입·청산 파라미터는 재튜닝하지 않았습니다. 생존 편향이 남아 절대 수익은 낙관적입니다.",
        "",
    ]
    for title, key, note in (
        ("2018–2022 (주 검증)", "main", f"종목 범위 {report['main']['universe_size']}개 (2017년 거래대금 상위 {TOP_N} + 기본)"),
        (
            f"{HOLDOUT_START}년 이후 — 봉인 홀드아웃 (참고만, 튜닝 금지)",
            "holdout",
            f"종목 범위 {report['holdout']['universe_size']}개 (2022년 거래대금 상위 {TOP_N} + 기본) · 2023-10 이후는 예전 튜닝 표본과 겹침",
        ),
    ):
        sec = report[key]
        lines += [f"## {title}", "", note, ""]
        for metric, label in (("ret", "수익률"), ("mdd", "최대 낙폭 (MDD)")):
            lines += [f"### {label}", "", "| 구간 | 약세일 | " + " | ".join(COLUMNS.values()) + " |", "|---|---|" + "---|" * len(COLUMNS)]
            for name, w in sec["windows"].items():
                lines.append(f"| {name} | {w['weak_days_pct']}% | " + " | ".join(_cell(w[c][metric]) for c in COLUMNS) + " |")
            lines.append("")
        for alt in sec.get("sensitivity", []):
            w = alt["window"]
            lines += [
                f"범위 민감도 — {alt['label']} ({alt['universe_size']}종목, 차이: {alt['diff']}): "
                + " · ".join(f"{COLUMNS[c]} {_cell(w[c]['ret'])}/{_cell(w[c]['mdd'])}" for c in COLUMNS),
                "",
            ]
        cum = sec["windows"][sec["cumulative"]]
        lines += [f"### 누적 ({sec['cumulative']}) 세부", "", "| 전략 | CAGR | Sharpe | 청산 매매 | 경고 |", "|---|---|---|---|---|"]
        for c in ("baseline_mom", "new_mom", "baseline_acct", "new_acct"):
            r = cum[c]
            sharpe = "—" if r["sharpe"] is None else f"{r['sharpe']:.2f}"
            lines.append(f"| {COLUMNS[c]} | {_cell(r['cagr'])} | {sharpe} | {r['trades']} | {' / '.join(r['warnings']) or '—'} |")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=HERE / "data" / "long")
    ap.add_argument("--out", type=Path, default=HERE / "results")
    args = ap.parse_args()
    raw, bench, b = load(args.data)
    core = json.loads((HERE / "universe_candidates.json").read_text())["core22"]
    last = bench.index[-1]
    report: dict[str, Any] = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "data_range": f"{bench.index[0].date()}~{last.date()}",
        "bench_source": b.get("source"),
        "candidates": len(raw),
    }
    sections = (
        (
            "main",
            MAIN_YEARS[0] - 1,
            [(str(y), f"{y}-01-01", f"{y}-12-31") for y in MAIN_YEARS],
            (f"{MAIN_YEARS[0]}–{str(MAIN_YEARS[-1])[2:]}", f"{MAIN_YEARS[0]}-01-01", f"{MAIN_YEARS[-1]}-12-31"),
        ),
        (
            "holdout",
            HOLDOUT_START - 1,
            [(str(y), f"{y}-01-01", f"{y}-12-31") for y in range(HOLDOUT_START, last.year + 1)],
            (f"{HOLDOUT_START}–{last:%y.%m}", f"{HOLDOUT_START}-01-01", last.strftime("%Y-%m-%d")),
        ),
    )
    for key, year_before, years, cum in sections:
        uni = pick_universe(raw, year_before, core)
        frames = {s: indicators(raw[s]) for s in uni}
        windows = {name: run_window(frames, bench, start, end) for name, start, end in [*years, cum]}
        report[key] = {"universe_size": len(uni), "universe": uni, "cumulative": cum[0], "windows": windows}
        if key == "main":
            # 종목 하나만 달라도 누적 결과가 크게 흔들려, 상장 기준일을 1년 앞당긴 범위로도 누적 구간을 돌려 둡니다.
            alt = pick_universe(raw, year_before, core, listed_by=f"{year_before - 1}-06-30")
            alt_frames = {s: indicators(raw[s]) for s in alt}
            diff = f"+{sorted(set(alt) - set(uni))} −{sorted(set(uni) - set(alt))}"
            report[key]["sensitivity"] = [{"label": f"{year_before - 1}-06-30 이전 상장 기준", "universe_size": len(alt), "diff": diff, "window": run_window(alt_frames, bench, cum[1], cum[2])}]
        print(f"[{key}] 범위 {len(uni)}종목")
        for name, w in windows.items():
            print(f"  {name:>8} 약세 {w['weak_days_pct']:>3}% | " + " | ".join(f"{c} {_cell(w[c]['ret'])}/{_cell(w[c]['mdd'])}" for c in COLUMNS))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "long_check.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    (args.out / "long_check.md").write_text(markdown(report))
    print(f"저장: {args.out / 'long_check.md'}, {args.out / 'long_check.json'}")


if __name__ == "__main__":
    main()
