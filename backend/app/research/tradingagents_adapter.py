"""원본 TradingAgents 를 쓸 때의 어댑터 (RESEARCH_ENGINE=tradingagents).

TradingAgents 는 PyPI 에 없어 GitHub 소스로 따로 설치해야 합니다:
    uv pip install "tradingagents @ git+https://github.com/TauricResearch/TradingAgents.git"
설치되어 있지 않으면 daily.py 가 내장 파이프라인(pipeline.py)으로 돌아갑니다.
원본은 Yahoo Finance 시세를 쓰므로 국내 종목은 '.KS'(코스피)·'.KQ'(코스닥) 접미사를 붙입니다.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from app.research.pipeline import RATINGS, ResearchResult


def available() -> bool:
    return importlib.util.find_spec("tradingagents") is not None


def run(symbol: str, name: str, market: str, as_of: str, quick: str, deep: str, work_dir: Path) -> ResearchResult:
    from tradingagents.default_config import DEFAULT_CONFIG  # type: ignore[import-not-found]
    from tradingagents.graph.trading_graph import TradingAgentsGraph  # type: ignore[import-not-found]

    cfg = DEFAULT_CONFIG.copy()
    cfg.update(
        llm_provider=os.getenv("TRADINGAGENTS_LLM_PROVIDER", "openai"),
        quick_think_llm=quick,
        deep_think_llm=deep,
        max_debate_rounds=1,
        max_risk_discuss_rounds=1,
        output_language="Korean",
        results_dir=str(work_dir / "logs"),
        data_cache_dir=str(work_dir / "cache"),
        memory_log_path=str(work_dir / "memory" / "trading_memory.md"),
    )
    ticker = f"{symbol}.{'KQ' if market == 'KOSDAQ' else 'KS'}"
    state, decision = TradingAgentsGraph(config=cfg).propagate(ticker, as_of)
    text = str(decision)
    rating = next((r for r in RATINGS if r.lower() in text.lower()), "Hold")
    report = state.get("final_trade_decision") if isinstance(state, dict) else None
    md = f"# {name} ({symbol}) 심층 리서치 · {as_of}\n\n**최종 등급: {rating}** (TradingAgents)\n\n{report or text}\n"
    return ResearchResult(symbol, name, rating, 0.5, text.strip().splitlines()[0][:200] if text.strip() else rating, md, f"TradingAgents {quick} / {deep}", engine="tradingagents")
