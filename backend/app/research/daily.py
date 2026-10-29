"""장 시작 전 심층 리서치 배치.

대상: AI가 보유 중인 종목 + 모멘텀 종목 범위 안의 '돌파 임박' 종목 (최대 RESEARCH_MAX_SYMBOLS 개).
돌파 임박은 전날 종가가 최근 20일 최고 종가의 NEAR_HIGH_PCT 안이고 60일선 위인 종목입니다. 그날 장 마감 판단 구간에
실제로 돌파 신호가 날 만한 종목을 미리 분석해 둬야 약세 의견(Underweight·Sell) 감점이 진입 후보에 걸립니다.
실행: `uv run python -m app.research.daily [--symbols 005930,000660] [--force]`
결과: data/research/<날짜>/<종목>.md 와 data/research/latest.json — 엔진이 근거 시그널로 읽고,
Underweight·Sell 종목은 신규 매수 점수를 깎고 매수 금액을 줄입니다 (Sell 이 더 크게).
"""

from __future__ import annotations

import os
import argparse
import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.container import build_market
from app.db import make_session_factory, now_kst, session_scope
from app.market.calendar import is_trading_day
from app.market.universe import instrument, register, strategy_symbols, universe_symbols
from app.models import Position, Strategy
from app.quant.signals import indicators
from app.research import tradingagents_adapter
from app.research.pipeline import ResearchPipeline, ResearchResult

log = logging.getLogger("research.daily")

# pm2 가 프로세스를 처음 띄울 때도 한 번 실행되므로, 예약 시간대가 아니면 건너뜁니다 (--force 로 무시).
WINDOW = (6, 9)
# 돌파 임박: 전날 종가가 최근 20일(전날 포함) 최고 종가에서 이 비율 안
NEAR_HIGH_PCT = 0.03


def research_universe(settings: Any, market: Any, s: Any) -> list[str]:
    """운영 중인 모멘텀 전략의 종목 범위 (코스피200 은 시가총액 상위까지 넓힘). 모멘텀 전략이 없으면 코스피200, 투자유의 종목은 뺍니다."""
    strats = s.scalars(select(Strategy).where(Strategy.status != "stopped", Strategy.template == "momentum")).all()
    specs = {(st.universe, tuple(st.custom_symbols or ())) for st in strats} or {("k200", ())}
    symbols: set[str] = set()
    for universe, custom in specs:
        try:
            symbols |= set(strategy_symbols(universe, list(custom), market, settings.k200_universe_size))
        except Exception as exc:  # noqa: BLE001
            log.warning("시가총액 상위 종목 조회 실패, 기본 목록만 씁니다: %s", exc)
            symbols |= set(universe_symbols(universe, list(custom)))
    try:
        symbols -= market.risky_symbols()
    except Exception as exc:  # noqa: BLE001
        log.warning("투자유의 종목 조회 실패: %s", exc)
    return sorted(symbols)


def breakout_watchlist(market: Any, symbols: list[str]) -> list[str]:
    """20일 최고 종가에 NEAR_HIGH_PCT 안으로 다가섰고 60일선 위인 종목. 고점에 가까운 순서(같으면 거래량이 는 순서)."""
    scored: list[tuple[float, float, str]] = []
    for sym in symbols:
        try:
            ind = indicators(market.history(sym, 300))
        except Exception as exc:  # noqa: BLE001
            log.warning("시세 조회 실패 %s: %s", sym, exc)
            continue
        if len(ind) < 61:
            continue
        last = ind.iloc[-1]
        gap = float(last["close"] / ind["close"].iloc[-20:].max() - 1)
        if math.isnan(last["ma60"]) or last["close"] <= last["ma60"] or gap < -NEAR_HIGH_PCT:
            continue
        vol = float(last["vol_ratio"]) if not math.isnan(last["vol_ratio"]) else 0.0
        scored.append((gap, vol, sym))
    scored.sort(reverse=True)
    return [sym for _gap, _vol, sym in scored]


def pick_symbols(settings: Any, limit: int, market: Any) -> list[str]:
    """보유 종목 먼저, 그다음 모멘텀 돌파 임박 종목. 전날 ML 순위는 쓰지 않습니다 (진입 후보와 어긋나서)."""
    chosen: list[str] = []
    sf = make_session_factory(settings.database_url)
    with session_scope(sf) as s:
        for p in s.scalars(select(Position).order_by(Position.opened_at)).all():
            if p.symbol not in chosen:
                chosen.append(p.symbol)
        universe = research_universe(settings, market, s)
    for sym in breakout_watchlist(market, universe):
        if len(chosen) >= limit:
            break
        if sym not in chosen:
            chosen.append(sym)
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
    parser.add_argument("--symbols", default="", help="쉼표로 구분한 종목코드 (없으면 보유 + 모멘텀 돌파 임박)")
    parser.add_argument("--force", action="store_true", help="예약 시간대가 아니어도 실행")
    args = parser.parse_args()

    settings = get_settings()
    now = now_kst()
    if not args.force and not (WINDOW[0] <= now.hour < WINDOW[1] and now.weekday() < 5):
        log.info("예약 시간대(평일 %02d–%02d시)가 아니어서 건너뜁니다. 바로 돌리려면 --force", *WINDOW)
        return
    if not args.force and not is_trading_day(now.date()):
        log.info("KRX 휴장일이라 건너뜁니다. 바로 돌리려면 --force")
        return
    chatgpt_ta = (
        settings.research_engine == "tradingagents"
        and settings.tradingagents_llm_provider.lower() == "chatgpt"
        and tradingagents_adapter.available()
    )
    if chatgpt_ta and not (Path.home() / ".tradingagents" / "chatgpt-auth.json").exists():
        log.warning("ChatGPT 로그인 정보가 없어 심층 리서치를 건너뜁니다 (tradingagents login chatgpt)")
        return
    if not chatgpt_ta and not settings.openai_api_key:
        log.warning("OPENAI_API_KEY 가 없어 심층 리서치를 건너뜁니다")
        return
    market, _client = build_market(settings)
    listed = getattr(market, "listed_instruments", None)
    if listed is not None:
        try:
            register(listed())  # 넓힌 종목 범위의 이름
        except Exception as exc:  # noqa: BLE001
            log.warning("종목 리스트 조회 실패: %s", exc)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or pick_symbols(settings, settings.research_max_symbols, market)
    if not symbols:
        log.info("보유 종목도 돌파 임박 종목도 없어 심층 리서치를 건너뜁니다")
        return
    log.info("심층 리서치 대상: %s", ", ".join(f"{x} {instrument(x).name}" for x in symbols))
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
                result = tradingagents_adapter.run(sym, inst.name, inst.market, as_of, settings.research_quick_model, deep, out_dir / "_tradingagents", settings.tradingagents_llm_provider)
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
