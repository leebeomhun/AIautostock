# AutoStock — AI 자동매매

퀀트 규칙이 후보를 고르고, OpenAI가 근거를 읽어 최종 판단과 설명을 붙이며, 키움증권 REST API로 주문합니다.
디자인 시안(AI 자동매매 UI/UX 예시)의 다섯 화면과 UX 원칙을 그대로 구현했습니다.

- 상태와 통제권은 항상 보이게: 모든 화면 상단에 AI 상태, 모드, 계좌 종류, 긴급 정지
- 모든 주문에 '왜'를 붙이기: 근거 시그널, 신뢰도, 과거 유사 신호 통계, 계좌 영향, 판단 로그
- 자동화 수준은 사용자가 고르기: 알림만 → 승인 후 주문 → 완전 자동, 모의투자로 시작
- 한국 색상 관례: 상승·매수 빨강 ▲, 하락·매도 파랑 ▼

## 구조

```
backend/   FastAPI · SQLAlchemy(SQLite) · pandas · OpenAI SDK
  app/quant/timeseries.py  gs-quant 시계열 함수와 같은 정의 (RSI, volatility, max_drawdown, zscores …)
  app/quant/signals.py     전략별 진입·청산 시그널과 근거 문장 (평균회귀는 gs-quant MeanReversionTrigger 방식)
  app/quant/backtest.py    트리거→액션 일봉 백테스트 (종가 판단, 다음 날 시가 체결)
  app/ai/analyst.py        OpenAI Responses API 구조화 출력으로 최종 판단, web_search 로 뉴스 감성
  app/kiwoom/              키움 REST 클라이언트(토큰·유량·연속조회), 시세, 주문·잔고·체결
  app/trading/engine.py    엔진: 준비(잠금) → AI 판단(잠금 해제) → 반영(잠금)
  app/trading/risk.py      일일 손실 한도 50/80/100%, 종목 비중, 하루 주문 수
frontend/  Vite · React 19 · TypeScript · react-router
```

## 실행

```bash
cp .env.example .env    # 키 입력 (이미 .env 가 있으면 건너뜀)

# 백엔드
cd backend
uv sync
uv run uvicorn app.main:app --port 8000

# 프런트엔드 (개발 서버, /api 는 8000 으로 프록시)
cd frontend
npm install
npm run dev
```

배포 시에는 `npm run build` 후 백엔드만 띄우면 `frontend/dist` 를 함께 서빙합니다.

키움 키가 없으면 로컬 시뮬레이션(모의 시세 + 로컬 모의 계좌)으로, OpenAI 키가 없으면 규칙 기반 설명으로 동작합니다.
화면의 계좌 배지(모의투자 / 실전투자 / 로컬 시뮬레이션)와 판단 기록의 'AI 모델'에서 어느 경로로 돌고 있는지 확인할 수 있습니다.

## 배포 (kiwoom.codesearch.ai.kr)

- nginx `kiwoom.codesearch.ai.kr` → `127.0.0.1:8012` (SSL 은 기존 Certbot 인증서). 이전 설정은 `/etc/nginx/sites-available/kiwoom.codesearch.ai.kr.bak-20261028`.
- pm2 (`ecosystem.config.cjs`):
  - `autostock` — 웹 + 매매 엔진 (반드시 1개 인스턴스)
  - `autostock-ml` — 평일 18:40 야간 ML 점수 (장중에는 건너뜀)
  - `autostock-research` — 평일 07:20 심층 리서치 (06–09시 밖에서는 건너뜀)
- 프런트를 바꾼 뒤: `cd frontend && npm run build && pm2 reload autostock`
- 로그인: `.env` 의 `APP_PASSWORD` (세션 쿠키 30일, 실패 5회면 10분 잠금). 비어 있으면 로그인 없이 열리니 공개 도메인에서는 꼭 설정하세요.
- PWA: `manifest.webmanifest` + `sw.js`. 앱 화면만 캐시하고 `/api` 응답(시세·주문)은 캐시하지 않습니다.

## 보조 판단 (배치)

- **ML 점수** `python -m app.ml.nightly` — qlib Alpha158 팩터 정의(157개, VWAP 제외)를 옮기고 LightGBM 으로 다음 거래일 순위를 학습합니다.
  qlib 의 RobustZScoreNorm·CSRankNorm 순서를 따릅니다. 최근 60일 검증 순위 IC 가 0.02 미만이면 판단 점수에 반영하지 않고 참고로만 보여 줍니다.
- **심층 리서치** `python -m app.research.daily [--symbols 005930] [--force]` — TradingAgents 의 역할 구성(기술·뉴스·펀더멘털 애널리스트 → 강세/약세 토론 → 포트폴리오 매니저 5단계 등급)을
  OpenAI 로 구현했습니다. 대상은 보유 종목 + 모멘텀 종목 범위 안의 돌파 임박 종목(전날 종가가 20일 최고 종가 3% 이내·60일선 위),
  최대 `RESEARCH_MAX_SYMBOLS`(기본 5), 종목당 OpenAI 호출 6회 안팎. 전날 ML 순위는 대상 선정에 쓰지 않습니다.
  Underweight·Sell 종목은 신규 매수를 막지 않고 점수를 깎고 매수 금액을 줄입니다 (Underweight 50%, Sell 25%). 원본 TradingAgents 를 설치하고 `RESEARCH_ENGINE=tradingagents` 로 바꾸면 원본을 씁니다.
- qlib·TradingAgents 원본 패키지는 이 서버(aarch64)에서 GitHub 소스 설치가 필요해 기본 구성에는 넣지 않았습니다.

## 보호 규칙 · 판단 원장

- 보호 규칙 (freqtrade Protections 개념): 손절한 종목 3거래일 재진입 금지, 청산 후 1거래일 대기, 하루 손절 3회면 신규 매수 중지, 20일 고점 대비 −10% 낙폭이면 신규 매수 중지. `PROTECT_*` 로 조정.
- 판단 원장 (해시 체인): 판단 생성·승인·거절·만료·주문·체결·모드 변경·긴급 정지를 순서대로 기록하고 각 기록에 직전 해시를 넣습니다. `GET /api/ledger/verify` 로 변조 여부를 검사합니다.

## 매매 흐름

1. 엔진이 `ENGINE_INTERVAL_SECONDS` 마다 장중(KRX 09:00–15:30)에 전략별 종목을 살핍니다.
2. 퀀트 규칙이 후보와 근거 수치를 만들고, 리스크 가드가 수량을 정합니다.
3. OpenAI가 후보를 따르거나 보류만 할 수 있습니다. 반대 방향 주문, 손절 보류, 제안 수량 초과는 코드에서 막습니다.
4. 모드에 따라 알림 / 승인 대기(기본 5분 후 자동 취소) / 즉시 주문합니다.
5. 체결은 키움 체결·미체결 조회로 맞추고, AI가 연 포지션만 전략 장부에 기록합니다. 사용자가 직접 산 종목은 AI가 팔지 않습니다.

### 모멘텀 돌파 규칙 (장 마감 판단 구간 `CLOSE_WINDOW`)

- 종목 범위: 코스피200 은 기본 22종목 + 키움 코스피 시가총액 상위 `K200_UNIVERSE_SIZE`(기본 40). 투자유의 종목은 뺍니다.
- 진입: 종가 > 직전 20일 고점, 거래량 20일 평균의 1.4배 이상, 60일선 위. KOSPI 가 60일선 아래면 매수 금액 50%.
- 정렬 가산: ML 순위(검증 IC 통과 시, 상위 20% 추가 가산), 기관·외국인 동반 2일 이상 연속 순매수(키움 ka10131, +0.03). 리서치 약세는 감점.
- 청산: 손절·익절(절반) · 일부 익절 뒤 최고 종가 대비 트레일링(손절폭 2–3배, 변동성 맞춤) · 20일선 이탈 · 10거래일에 +3% 미만.
- 업종 쏠림 제한 `MAX_POSITIONS_PER_SECTOR` 은 백테스트에서 수익만 줄어 기본 끔(0)입니다.

## 운영 점검

- `uv run python -m app.ops [--days 30]` — 모의 체결 평균가와 그날 종가의 차이(백테스트는 종가 체결 가정)와 ML 판단 반영 여부를 출력합니다.
  15:40(종가 확정) 뒤에 보고, 평균 불리 폭이 슬리피지 가정(`SLIPPAGE_RATE`, 0.05%)보다 크면 백테스트 성과를 그만큼 깎아 봅니다.
- 18:40 ML 배치 뒤: `pm2 logs autostock-ml --lines 20` 의 `구간별 [...]` 과 `판단 반영: 예/아니오` 줄을 확인합니다.
  검증 순위 IC 평균 0.02 이상이고 3개 구간 중 2개 이상 양수일 때만 엔진이 ML 점수를 씁니다 (`Insights.ml_gate`).
- 전략 배분 변경: 설정 화면 '배분 변경' 또는 `PATCH /api/strategies/{id}` `{"allocation": 7000000}`. 보유 종목은 팔지 않고 신규 매수 한도만 바뀝니다.

## 안전장치

- 기본은 `KIWOOM_MODE=mock` (모의투자 서버). 실전 키는 `KIWOOM_MODE=real` 일 때만 씁니다.
- 실전 계좌로 연결해도 설정 화면에서 '실전 주문 허용'을 하기 전까지는 알림만 보냅니다. 허용은 `PAPER_DAYS_BEFORE_LIVE`(기본 14일) 운영 후 열립니다.
- 긴급 정지: 승인 대기를 취소하고 미체결 주문에 취소를 요청합니다.
- 일일 손실 한도: 50% 알림, 80% 신규 매수 절반, 100% 신규 매수 중지(또는 모든 매매 중지).
- KRX 휴장일(공휴일·대체공휴일·선거일·12/31)에는 판단·주문하지 않습니다. 목록(`app/market/calendar.py`)은 2027-12-31까지이고 그 뒤는 갱신 전까지 휴장으로 봅니다.
  임시공휴일은 `KRX_EXTRA_HOLIDAYS=2026-11-02,...` 로 더합니다.
- 수수료·세율(`FEE_RATE`, `SELL_TAX_RATE`)은 백테스트와 로컬 시뮬레이션용 가정치입니다. 실제 체결 비용은 키움 체결 내역 값을 씁니다.

AI의 매매 신호는 수익을 보장하지 않으며, 투자 판단과 결과의 책임은 투자자 본인에게 있습니다.

## 테스트

```bash
cd backend && uv run pytest
cd frontend && npm run build
```
