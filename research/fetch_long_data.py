"""장기 진단 백테스트(research/long_check.py)용 일봉 받기. 운영 앱·키움과 무관합니다.

- 종목 일봉: pykrx 의 네이버 수정주가 경로 (KRX 엔드포인트 — 과거 시점 종목 목록·시가총액·지수 — 는 KRX 로그인이 필요해 쓰지 않음)
- KOSPI 지수: yfinance ^KS11
pykrx·yfinance 는 운영 backend/.venv 에 없습니다. 별도 가상환경에서 실행하세요:
    uv venv /tmp/longvenv --python 3.12 && uv pip install --python /tmp/longvenv/bin/python pykrx yfinance
    /tmp/longvenv/bin/python research/fetch_long_data.py [--start 2016-01-01] [--end 오늘] [--out research/data/long]
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--out", type=Path, default=HERE / "data" / "long")
    args = ap.parse_args()

    import yfinance as yf
    from pykrx import stock

    cand = json.loads((HERE / "universe_candidates.json").read_text())
    syms = sorted(set(cand["top200"]) | set(cand["core22"]))
    (args.out / "cache").mkdir(parents=True, exist_ok=True)
    s, e = args.start.replace("-", ""), args.end.replace("-", "")
    fail: dict[str, str] = {}
    for k, sym in enumerate(syms):
        f = args.out / "cache" / f"{sym}.json"
        if f.exists():
            continue
        try:
            df = stock.get_market_ohlcv_by_date(s, e, sym, adjusted=True)
            df = df[df["종가"] > 0] if df is not None and not df.empty else None
            if df is None or df.empty:
                fail[sym] = "empty"
            else:
                cols = {"open": "시가", "high": "고가", "low": "저가", "close": "종가", "volume": "거래량"}
                f.write_text(json.dumps({"dates": [d.strftime("%Y-%m-%d") for d in df.index], **{k2: df[v].astype(float).tolist() for k2, v in cols.items()}}))
        except Exception as exc:  # noqa: BLE001
            fail[sym] = f"{type(exc).__name__}: {exc}"[:160]
        if k % 25 == 0:
            print(k, sym, flush=True)
        time.sleep(1.0)  # pykrx README 권고 간격
    end_excl = (date.fromisoformat(args.end) + timedelta(days=1)).isoformat()  # yfinance 의 end 는 그날을 빼므로 하루 뒤
    k = yf.download("^KS11", start=args.start, end=end_excl, progress=False, auto_adjust=False)
    close = k["Close"].squeeze().dropna()
    (args.out / "bench.json").write_text(json.dumps({"source": "yfinance ^KS11", "close": {d.strftime("%Y-%m-%d"): float(v) for d, v in close.items()}}))
    (args.out / "meta.json").write_text(json.dumps({"start": args.start, "end": args.end, "symbols": len(syms), "fail": fail}, ensure_ascii=False))
    print(f"받음 {len(list((args.out / 'cache').glob('*.json')))} · 실패 {len(fail)} · 지수 {len(close)}일 {close.index[0].date()}~{close.index[-1].date()}", flush=True)


if __name__ == "__main__":
    main()
