"""심층 리서치 — TradingAgents(Tauric Research, Apache-2.0)의 역할 구성을 OpenAI 로 직접 구현했습니다.

애널리스트(기술·뉴스·펀더멘털) → 강세/약세 연구원 토론 → 포트폴리오 매니저의 5단계 등급
(Buy / Overweight / Hold / Underweight / Sell). 원본은 미국 데이터(Yahoo·SEC) 위주라, 여기서는
키움 일봉·기본정보와 OpenAI 웹 검색(국내 뉴스·DART 공시)을 씁니다.
모든 단계는 Responses API 구조화 출력이고, 숫자는 입력으로 준 것만 쓰도록 지시합니다.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel

from app.quant.signals import indicators

log = logging.getLogger(__name__)

RATINGS = ("Buy", "Overweight", "Hold", "Underweight", "Sell")

COMMON = """너는 한국 주식 리서치 팀의 일원이다. 한국어로 쓴다.
- 입력에 있는 숫자만 인용한다. 입력에 없는 재무 수치·목표가·뉴스를 지어내지 않는다.
- 수익을 보장하는 표현을 쓰지 않는다.
- 문장은 짧고 평이하게 쓴다."""

MARKET_ROLE = COMMON + "\n역할: 기술적 분석가. 가격·거래량 지표로 추세, 모멘텀, 변동성, 지지·저항을 판단한다."
NEWS_ROLE = COMMON + """
역할: 뉴스·공시 분석가. 웹 검색으로 이 종목의 최근 7일 국내 기사와 DART 공시를 찾아 주가에 영향을 줄 사건을 정리한다.
찾은 기사·공시의 제목과 주소만 sources 에 넣는다. 찾지 못했으면 stance 는 neutral, report 에 '확인된 뉴스 없음'이라고 쓴다."""
FUNDAMENTAL_ROLE = COMMON + "\n역할: 펀더멘털 분석가. 주어진 밸류에이션·실적 항목으로 저평가·고평가와 이익의 질을 판단한다. 항목이 비어 있으면 판단을 보류한다."
BULL_ROLE = COMMON + "\n역할: 강세 연구원. 세 보고서를 근거로 이 종목을 사야 하는 이유를 가장 설득력 있게 주장한다. 약점도 인정하되 반박한다."
BEAR_ROLE = COMMON + "\n역할: 약세 연구원. 세 보고서와 강세 측 주장을 반박하며 이 종목을 피하거나 줄여야 하는 이유를 주장한다."
MANAGER_ROLE = COMMON + """
역할: 포트폴리오 매니저. 애널리스트 보고서와 강세·약세 토론, 현재 보유 상황을 종합해 최종 등급을 정한다.
rating 은 Buy, Overweight, Hold, Underweight, Sell 중 하나다. 근거가 엇갈리면 Hold 를 택한다.
summary 는 투자자에게 보여 줄 한 문장, thesis 는 3~5문장, risks 는 핵심 위험 2~4개."""


class AnalystReport(BaseModel):
    stance: Literal["bullish", "neutral", "bearish"]
    key_points: list[str]
    report: str


class NewsSource(BaseModel):
    title: str
    url: str


class NewsReport(AnalystReport):
    # 구조화 출력(strict)은 자유 형식 dict 를 받지 않아 출처도 필드를 정해 둡니다.
    sources: list[NewsSource]


class DebateTurn(BaseModel):
    argument: str
    key_points: list[str]


class FinalDecision(BaseModel):
    rating: Literal["Buy", "Overweight", "Hold", "Underweight", "Sell"]
    confidence: float
    summary: str
    thesis: str
    risks: list[str]
    horizon_days: int


@dataclass
class ResearchResult:
    symbol: str
    name: str
    rating: str
    confidence: float
    summary: str
    markdown: str
    model: str
    engine: str = "builtin"
    calls: int = 0
    sources: list[dict[str, str]] = field(default_factory=list)


def technical_snapshot(df: pd.DataFrame) -> dict[str, Any]:
    ind = indicators(df)
    row = ind.iloc[-1]

    def r(v: float, d: int = 2) -> float | None:
        return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), d)

    close = df["close"]
    tail = close.iloc[-60:]
    return {
        "기준일": df.index[-1].strftime("%Y-%m-%d"),
        "종가": r(row["close"], 0),
        "20일 평균": r(row["ma20"], 0),
        "60일 평균": r(row["ma60"], 0),
        "250일 평균": r(row["ma250"], 0),
        "RSI(14)": r(row["rsi14"], 1),
        "20일 z-score": r(row["z20"]),
        "20일 변동성(연환산 %)": r(row["vol20"], 1),
        "5일 수익률(%)": r((close.iloc[-1] / close.iloc[-6] - 1) * 100) if len(close) > 6 else None,
        "20일 수익률(%)": r((close.iloc[-1] / close.iloc[-21] - 1) * 100) if len(close) > 21 else None,
        "60일 수익률(%)": r(row["ret60"] * 100),
        "52주 최고": r(close.iloc[-250:].max(), 0),
        "52주 최저": r(close.iloc[-250:].min(), 0),
        "거래량(20일 평균 대비 배)": r(row["vol_ratio"]),
        "최근 60거래일 종가(10일 간격)": [r(v, 0) for v in tail.iloc[::10]],
    }


class ResearchPipeline:
    def __init__(self, api_key: str, quick_model: str, deep_model: str, reasoning_effort: str = "low") -> None:
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, timeout=180, max_retries=2)
        self.quick = quick_model
        self.deep = deep_model
        self.effort = reasoning_effort
        self.calls = 0

    def _ask(self, model: str, role: str, payload: dict[str, Any], fmt: type[BaseModel], web: bool = False) -> Any:
        kwargs: dict[str, Any] = {}
        if web:
            kwargs["tools"] = [{"type": "web_search", "search_context_size": "medium"}]
            kwargs["include"] = ["web_search_call.action.sources"]
        resp = self.client.responses.parse(
            model=model,
            reasoning={"effort": self.effort},
            instructions=role,
            input=json.dumps(payload, ensure_ascii=False, default=str),
            text_format=fmt,
            max_output_tokens=6000,
            store=False,
            **kwargs,
        )
        self.calls += 1
        parsed = resp.output_parsed
        if parsed is None:
            raise RuntimeError(f"{fmt.__name__} 응답이 비어 있습니다")
        return parsed

    def run(self, symbol: str, name: str, market: str, history: pd.DataFrame, fundamentals: dict[str, Any], position: dict[str, Any] | None, as_of: str) -> ResearchResult:
        self.calls = 0
        base = {"종목": name, "종목코드": symbol, "시장": market, "분석 기준일": as_of}
        tech = technical_snapshot(history)
        market_r = self._ask(self.quick, MARKET_ROLE, base | {"지표": tech}, AnalystReport)
        news_r = self._ask(self.quick, NEWS_ROLE, base | {"요청": f"{name}({symbol}) 최근 7일 국내 뉴스와 DART 공시"}, NewsReport, web=True)
        fund_r = self._ask(self.quick, FUNDAMENTAL_ROLE, base | {"키움 기본정보": fundamentals or "제공되지 않음"}, AnalystReport)
        reports = {
            "기술적 분석": market_r.model_dump(),
            "뉴스·공시": news_r.model_dump(exclude={"sources"}),
            "펀더멘털": fund_r.model_dump(),
        }
        bull = self._ask(self.quick, BULL_ROLE, base | {"보고서": reports}, DebateTurn)
        bear = self._ask(self.quick, BEAR_ROLE, base | {"보고서": reports, "강세 측 주장": bull.argument}, DebateTurn)
        final = self._ask(
            self.deep,
            MANAGER_ROLE,
            base | {"보고서": reports, "강세": bull.model_dump(), "약세": bear.model_dump(), "보유 상황": position or "보유하지 않음"},
            FinalDecision,
        )
        sources = [{"title": x.title, "url": x.url} for x in news_r.sources if x.url.startswith("http")][:8]
        md = render_markdown(name, symbol, as_of, tech, market_r, news_r, fund_r, bull, bear, final, sources, self.quick, self.deep)
        return ResearchResult(
            symbol=symbol,
            name=name,
            rating=final.rating,
            confidence=max(0.0, min(1.0, final.confidence)),
            summary=final.summary.strip(),
            markdown=md,
            model=f"{self.quick} / {self.deep}",
            calls=self.calls,
            sources=sources,
        )


STANCE_KO = {"bullish": "긍정", "neutral": "중립", "bearish": "부정"}


def render_markdown(name, symbol, as_of, tech, market_r, news_r, fund_r, bull, bear, final, sources, quick, deep) -> str:  # noqa: ANN001
    def section(title: str, r: AnalystReport) -> str:
        points = "\n".join(f"- {p}" for p in r.key_points)
        return f"## {title} · {STANCE_KO[r.stance]}\n\n{points}\n\n{r.report}\n"

    risks = "\n".join(f"- {x}" for x in final.risks)
    src = "\n".join(f"- [{s.get('title') or s['url']}]({s['url']})" for s in sources) or "- 확인된 출처 없음"
    return f"""# {name} ({symbol}) 심층 리서치 · {as_of}

**최종 등급: {final.rating}** · 신뢰도 {round(final.confidence * 100)}% · 기간 약 {final.horizon_days}거래일

{final.summary}

{final.thesis}

### 핵심 위험
{risks}

{section("기술적 분석", market_r)}
{section("뉴스·공시", news_r)}
### 출처
{src}

{section("펀더멘털", fund_r)}
## 강세 측 주장

{bull.argument}

## 약세 측 주장

{bear.argument}

---
모델: 애널리스트·토론 {quick}, 매니저 {deep} · 구성은 TradingAgents(Tauric Research)를 따랐습니다.
이 리포트는 투자 권유가 아니며, 매매 판단과 결과의 책임은 투자자 본인에게 있습니다.
"""
