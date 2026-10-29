"""OpenAI 기반 매매 판단.

퀀트 규칙(app.quant.signals)이 만든 후보와 근거 수치를 받아, OpenAI Responses API 의 Structured Outputs
(client.responses.parse + Pydantic) 로 최종 판단·신뢰도·평이한 한국어 근거를 받습니다.

안전장치는 코드에서 강제합니다.
- AI 는 퀀트가 제안한 방향을 따르거나 보류(hold)만 할 수 있고, 반대 방향 주문은 낼 수 없습니다.
- 손절처럼 mandatory 인 규칙은 보류할 수 없습니다.
- 수량은 제안 수량을 넘지 못합니다.
API 키가 없거나 호출이 실패하면 규칙 기반 설명으로 대체하고, 판단 기록에 그 사실을 남깁니다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from app.quant.signals import Candidate, Signal, clamp, pct

log = logging.getLogger(__name__)

JUDGE_INSTRUCTIONS = """너는 한국 주식 자동매매 서비스에서 주문 직전 판단을 맡은 분석가다. 손실 관리가 수익보다 우선이다.

입력은 JSON 이다. candidate.proposed_action 은 퀀트 규칙이 낸 방향이고, signals 는 계산된 근거 수치다.

지켜야 할 규칙:
1. action 은 proposed_action 그대로이거나 "hold"(이번에는 주문하지 않음)만 가능하다. 반대 방향은 안 된다.
2. candidate.mandatory 가 true 이면(손절 규칙) action 은 반드시 proposed_action 이다. 근거 설명만 쓴다.
3. quantity_ratio 는 0~1 사이, 제안 수량 대비 비율이다. 근거가 엇갈리면 줄인다.
4. confidence 는 0~1 사이다. 근거가 같은 방향으로 모일수록 높게, 엇갈리면 낮게 둔다. 0.9 를 넘기지 않는다.
5. summary 는 투자자가 바로 이해할 평이한 한국어 1~2문장. 입력에 있는 숫자만 쓰고, 수익을 보장하는 표현은 쓰지 않는다.
6. signals 는 입력 signals 의 각 항목을 같은 key 로 다시 평가해 돌려준다. name·detail 은 다듬어도 되지만 입력에 없는 사실(뉴스, 실적, 공시 등)을 지어내지 않는다. side 는 buy/sell/hold/neutral, strength 는 0~1.
7. user_feedback 에 사용자가 비슷한 제안을 거절한 기록이 있으면 그 이유를 반영한다.
8. caution 은 주의할 점 한 문장. 없으면 빈 문자열.
9. 숫자는 읽기 쉽게 쓴다. 소수는 둘째 자리까지, 수익률·비율·적중률은 % 로(예: ret10 -0.0881 → −8.8%), 가격은 천 단위 쉼표와 '원'을 붙인다.
"""

NEWS_INSTRUCTIONS = """너는 한국 주식 뉴스·공시 감성 분석가다. 웹 검색으로 지정 종목의 최근 48시간 기사와 공시를 찾는다.
- 종목과 직접 관련된 기사만 고른다. 같은 내용의 중복 기사는 하나로 센다.
- 각 기사에 -1(매우 부정)~1(매우 긍정) 감성 점수를 매기고, score 는 관련성 가중 평균이다.
- 기사를 찾지 못했으면 article_count 0, score 0 으로 둔다. 기사를 지어내지 않는다.
- summary 는 한국어 한 문장. items 의 url 은 실제로 확인한 기사 주소다.
"""


class SignalOut(BaseModel):
    key: str
    name: str
    detail: str
    side: Literal["buy", "sell", "hold", "neutral"]
    strength: float


class Judgment(BaseModel):
    action: Literal["buy", "sell", "hold"]
    quantity_ratio: float
    confidence: float
    summary: str
    caution: str
    signals: list[SignalOut]


class NewsItem(BaseModel):
    title: str
    source: str
    url: str
    sentiment: float


class NewsSentiment(BaseModel):
    score: float
    article_count: int
    summary: str
    items: list[NewsItem]


@dataclass
class AIDecision:
    action: str
    quantity_ratio: float
    confidence: float
    summary: str
    caution: str
    signals: list[dict[str, Any]]
    model: str
    used_ai: bool
    error: str | None = None


@dataclass
class NewsResult:
    score: float
    article_count: int
    summary: str
    items: list[dict[str, str]] = field(default_factory=list)


def fallback_summary(c: Candidate, stop_pct: float, take_pct: float) -> str:
    m = c.metrics
    pnl = m.get("pnl_pct")
    texts = {
        "breakout": f"직전 20일 고점을 넘었고 거래량이 평소의 {m.get('vol_ratio', 0):.1f}배로 늘어 추세를 따라 매수를 제안합니다.",
        "meanrev_entry": "20일 평균보다 크게 내려간 뒤 반등을 시작해, 평균으로 돌아오는 구간을 노린 매수를 제안합니다.",
        "stop_loss": f"평균단가 대비 {pct((pnl or 0) / 100)}로 손절 기준(−{stop_pct:g}%)을 넘어 전량 매도를 제안합니다.",
        "take_profit": f"평균단가 대비 {pct((pnl or 0) / 100)}로 익절 기준(+{take_pct:g}%)에 닿아 절반을 팔아 수익을 확정합니다.",
        "trend_break": "종가가 20일 이동평균 아래로 내려가 상승 추세가 꺾인 것으로 보고 매도를 제안합니다.",
        "meanrev_exit": "가격이 20일 평균까지 돌아와 평균회귀 목표에 닿았습니다. 매도를 제안합니다.",
        "time_exit": "정해 둔 보유 기간이 지났는데 기대한 흐름이 나오지 않아 정리 매도를 제안합니다.",
        "rebalance": "이번 분기 가치 순위에서 빠져 리밸런싱 매도를 제안합니다.",
        "rebalance_buy": "이번 분기 가치 순위 상위에 들어 목표 비중만큼 매수를 제안합니다.",
        "news": "뉴스·공시 분위기가 긍정적으로 바뀌고 거래가 늘어 매수를 제안합니다.",
    }
    return texts.get(c.reason, "퀀트 규칙 신호에 따라 주문을 제안합니다.")


class AIAnalyst:
    def __init__(self, api_key: str | None, model: str, reasoning_effort: str = "low", news_search: bool = True) -> None:
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.news_search = news_search
        self._client = None
        if api_key:
            from openai import OpenAI

            self._client = OpenAI(api_key=api_key, timeout=60, max_retries=2)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @property
    def label(self) -> str:
        return self.model if self.enabled else "규칙 기반 (OpenAI 키 없음)"

    def _fallback(self, c: Candidate, stop_pct: float, take_pct: float, error: str | None = None) -> AIDecision:
        return AIDecision(
            action=c.side,
            quantity_ratio=1.0,
            confidence=round(clamp(c.score), 2),
            summary=fallback_summary(c, stop_pct, take_pct),
            caution="",
            signals=[s.as_dict() for s in c.signals],
            model="rule-based" if error is None else f"rule-based ({self.model} 호출 실패)",
            used_ai=False,
            error=error,
        )

    def judge(self, c: Candidate, context: dict[str, Any], stop_pct: float, take_pct: float) -> AIDecision:
        if not self._client:
            return self._fallback(c, stop_pct, take_pct)
        payload = {
            "candidate": {
                "symbol": c.symbol,
                "name": c.name,
                "proposed_action": c.side,
                "reason": c.reason,
                "mandatory": c.mandatory,
                "quant_score": round(c.score, 2),
                "proposed_qty": context.get("proposed_qty"),
                "metrics": c.metrics,
            },
            "signals": [s.as_dict() for s in c.signals],
            **context,
        }
        try:
            resp = self._client.responses.parse(
                model=self.model,
                reasoning={"effort": self.reasoning_effort},
                instructions=JUDGE_INSTRUCTIONS,
                input=json.dumps(payload, ensure_ascii=False, default=str),
                text_format=Judgment,
                max_output_tokens=4000,
                store=False,
            )
            parsed: Judgment | None = resp.output_parsed
            if parsed is None:
                raise ValueError("구조화 응답이 비어 있습니다")
        except Exception as exc:  # noqa: BLE001 — 어떤 실패든 규칙 기반으로 대체
            log.warning("OpenAI 판단 실패, 규칙 기반으로 대체: %s", exc)
            return self._fallback(c, stop_pct, take_pct, error=str(exc)[:200])

        action = parsed.action
        if action not in (c.side, "hold") or (c.mandatory and action != c.side):
            action = c.side
        by_key = {s.key: s for s in c.signals}
        signals: list[dict[str, Any]] = []
        for s in parsed.signals:
            base = by_key.get(s.key)
            if base is None:
                continue  # 입력에 없던 근거는 버립니다
            signals.append(Signal(base.key, s.name or base.name, s.detail or base.detail, s.side, clamp(s.strength)).as_dict())
        seen = {s["key"] for s in signals}
        signals.extend(s.as_dict() for s in c.signals if s.key not in seen)
        return AIDecision(
            action=action,
            quantity_ratio=clamp(parsed.quantity_ratio) if not c.mandatory else 1.0,
            confidence=round(min(clamp(parsed.confidence), 0.9), 2),
            summary=parsed.summary.strip() or fallback_summary(c, stop_pct, take_pct),
            caution=parsed.caution.strip(),
            signals=signals,
            model=self.model,
            used_ai=True,
        )

    def news_sentiment(self, symbol: str, name: str) -> NewsResult | None:
        """OpenAI web_search 도구로 최근 기사 감성을 매깁니다. 키가 없거나 꺼져 있으면 None."""
        if not self._client or not self.news_search:
            return None
        try:
            resp = self._client.responses.parse(
                model=self.model,
                reasoning={"effort": "low"},
                tools=[{"type": "web_search", "search_context_size": "low"}],
                include=["web_search_call.action.sources"],
                instructions=NEWS_INSTRUCTIONS,
                input=f"종목: {name} (KRX {symbol}). 최근 48시간 기사와 공시를 찾아 감성을 평가해 줘.",
                text_format=NewsSentiment,
                max_output_tokens=4000,
                store=False,
            )
            parsed: NewsSentiment | None = resp.output_parsed
            if parsed is None:
                return None
        except Exception as exc:  # noqa: BLE001
            log.warning("뉴스 감성 분석 실패 (%s): %s", symbol, exc)
            return None
        # 모델이 적은 주소 중 실제 검색 결과(출처·인용)에 있는 것만 화면에 보여 줍니다.
        cited = {_norm_url(u) for u in _citations(resp)}
        items = [
            {"title": i.title, "source": i.source, "url": i.url, "sentiment": round(clamp(i.sentiment, -1, 1), 2)}
            for i in parsed.items
            if i.url.startswith("http") and _norm_url(i.url) in cited
        ]
        return NewsResult(round(clamp(parsed.score, -1, 1), 2), len(items), parsed.summary, items[:6])


def _norm_url(url: str) -> str:
    return url.split("#")[0].split("?")[0].rstrip("/").lower()


def _citations(resp: Any) -> set[str]:
    urls: set[str] = set()
    for item in getattr(resp, "output", []) or []:
        if getattr(item, "type", None) == "web_search_call":
            action = getattr(item, "action", None)
            for src in getattr(action, "sources", None) or []:
                url = getattr(src, "url", None)
                if url:
                    urls.add(url)
        for content in getattr(item, "content", None) or []:
            for ann in getattr(content, "annotations", None) or []:
                url = getattr(ann, "url", None)
                if url:
                    urls.add(url)
    return urls
