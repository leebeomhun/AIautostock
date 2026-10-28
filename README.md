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

## 매매 흐름

1. 엔진이 `ENGINE_INTERVAL_SECONDS` 마다 장중(KRX 09:00–15:30)에 전략별 종목을 살핍니다.
2. 퀀트 규칙이 후보와 근거 수치를 만들고, 리스크 가드가 수량을 정합니다.
3. OpenAI가 후보를 따르거나 보류만 할 수 있습니다. 반대 방향 주문, 손절 보류, 제안 수량 초과는 코드에서 막습니다.
4. 모드에 따라 알림 / 승인 대기(기본 5분 후 자동 취소) / 즉시 주문합니다.
5. 체결은 키움 체결·미체결 조회로 맞추고, AI가 연 포지션만 전략 장부에 기록합니다. 사용자가 직접 산 종목은 AI가 팔지 않습니다.

## 안전장치

- 기본은 `KIWOOM_MODE=mock` (모의투자 서버). 실전 키는 `KIWOOM_MODE=real` 일 때만 씁니다.
- 실전 계좌로 연결해도 설정 화면에서 '실전 주문 허용'을 하기 전까지는 알림만 보냅니다. 허용은 `PAPER_DAYS_BEFORE_LIVE`(기본 14일) 운영 후 열립니다.
- 긴급 정지: 승인 대기를 취소하고 미체결 주문에 취소를 요청합니다.
- 일일 손실 한도: 50% 알림, 80% 신규 매수 절반, 100% 신규 매수 중지(또는 모든 매매 중지).
- 공휴일은 판단하지 않습니다. 휴장일에 낸 주문은 키움이 거부합니다.
- 수수료·세율(`FEE_RATE`, `SELL_TAX_RATE`)은 백테스트와 로컬 시뮬레이션용 가정치입니다. 실제 체결 비용은 키움 체결 내역 값을 씁니다.

AI의 매매 신호는 수익을 보장하지 않으며, 투자 판단과 결과의 책임은 투자자 본인에게 있습니다.

## 테스트

```bash
cd backend && uv run pytest
cd frontend && npm run build
```
