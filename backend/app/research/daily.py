"""장 시작 전 심층 리서치 배치.

대상: AI가 보유 중인 종목 + 전날 ML 순위 상위 종목 (최대 RESEARCH_MAX_SYMBOLS 개).
실행: `uv run python -m app.research.daily [--symbols 005930,000660] [--force]`
결과: data/research/<날짜>/<종목>.md 와 data/research/latest.json — 엔진이 근거 시그널로 읽고,
Underweight·Sell 종목은 신규 매수를 막습니다.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.container import build_market
from app.db import make_session_factory, now_kst, session_scope
from app.market.universe import UNIVERSES, instrument
from app.models import Position
from app.research import tradingagents_adapter
from app.research.pipeline import ResearchPipeline, ResearchResult

log = logging.getLogger("research.daily")

# pm2 가 프로세스를 처음 띄울 때도 한 번 실행되므로, 예약 시간대가 아니면 건너뜁니다 (--force 로 무시).
WINDOW = (6, 9)


def pick_symbols(settings: Any, limit: int) -> list[str]:
    chosen: list[str] = []
    sf = make_session_factory(settings.database_url)
    with session_scope(sf) as s:
        for p in s.scalars(select(Position).order_by(Position.opened_at)).all():
            if p.symbol not in chosen:
                chosen.append(p.symbol)
    ml_path = settings.data_dir / "ml" / "latest.json"
    domestic = {i.symbol for u in UNIVERSES.values() if u["domestic"] for i in u["instruments"]}
    try:
        scores = json.loads(ml_path.read_text()).get("scores", {})
    except (OSError, ValueError):
        scores = {}
    for sym, _ in sorted(scores.items(), key=lambda kv: kv[1]["rank"]):
        if sym in domestic and sym not in chosen:
            chosen.append(sym)
    if not scores:
        chosen += [x for x in ("005930", "000660", "005380") if x not in chosen]
    return chosen[:limit]


def save(result: ResearchResult, out_dir: Path, as_of: str) -> None:
    day = out_dir / as_of
    day.mkdir(parents=True, exist_ok=True)
    (day / f"{result.symbol}.md").write_text(result.markdown)
    index_path = out_dir / "latest.json"
    try:
        index = json.loads(index_path.read_text())
    except (OSError, ValueError):
        index = {"items": {}}
    index["items"][result.symbol] = {
        "symbol": result.symbol,
        "name": result.name,
        "rating": result.rating,
        "confidence": round(result.confidence, 2),
        "summary": result.summary,
        "as_of": as_of,
        "report": f"{as_of}/{result.symbol}.md",
        "model": result.model,
        "engine": result.engine,
        "sources": result.sources,
    }
    index["as_of"] = as_of
    index["model"] = result.model
    tmp = out_dir / "latest.json.tmp"
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1))
    tmp.replace(index_path)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="", help="쉼표로 구분한 종목코드 (없으면 보유 + ML 상위)")
    parser.add_argument("--force", action="store_true", help="예약 시간대가 아니어도 실행")
    args = parser.parse_args()

    settings = get_settings()
    now = now_kst()
    if not args.force and not (WINDOW[0] <= now.hour < WINDOW[1] and now.weekday() < 5):
        log.info("예약 시간대(평일 %02d–%02d시)가 아니어서 건너뜁니다. 바로 돌리려면 --force", *WINDOW)
        return
    if not settings.openai_api_key:
        log.warning("OPENAI_API_KEY 가 없어 심층 리서치를 건너뜁니다")
        return
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or pick_symbols(settings, settings.research_max_symbols)
    market, _client = build_market(settings)
    deep = settings.research_deep_model or settings.openai_model
    use_ta = settings.research_engine == "tradingagents" and tradingagents_adapter.available()
    if settings.research_engine == "tradingagents" and not use_ta:
        log.warning("RESEARCH_ENGINE=tradingagents 이지만 패키지가 없어 내장 파이프라인을 씁니다")
    pipeline = None if use_ta else ResearchPipeline(settings.openai_api_key, settings.research_quick_model, deep, settings.openai_reasoning_effort)
    out_dir = settings.data_dir / "research"
    as_of = now.strftime("%Y-%m-%d")
    started = datetime.now()
    total_calls = 0
    for sym in symbols:
        inst = instrument(sym)
        try:
            if use_ta:
                result = tradingagents_adapter.run(sym, inst.name, inst.market, as_of, settings.research_quick_model, deep, out_dir / "_tradingagents")
            else:
                history = market.history(sym, 300)
                info = getattr(market, "stock_info", lambda _s: {})(sym)
                result = pipeline.run(sym, inst.name, inst.market, history, info, None, as_of)
                total_calls += result.calls
        except Exception as exc:  # noqa: BLE001 — 한 종목이 실패해도 나머지는 계속
            log.warning("%s 리서치 실패: %s", sym, exc)
            continue
        save(result, out_dir, as_of)
        log.info("%s %s → %s (신뢰도 %.0f%%) · %s", sym, inst.name, result.rating, result.confidence * 100, result.summary)
    log.info("완료 %d종목 · OpenAI 호출 %d회 · %s", len(symbols), total_calls, datetime.now() - started)


if __name__ == "__main__":
    main()
