// pm2 설정 — `pm2 start ecosystem.config.cjs`
const PY = '/home/ubuntu/autostock/backend/.venv/bin/python'
const CWD = '/home/ubuntu/autostock/backend'
const ENV = { PYTHONUNBUFFERED: '1', TZ: 'Asia/Seoul' }

module.exports = {
  apps: [
    {
      // 웹 + 매매 엔진. 엔진이 프로세스 안에서 돌기 때문에 인스턴스는 반드시 1개 (여러 개면 주문이 중복됩니다).
      name: 'autostock',
      cwd: CWD,
      script: PY,
      args: '-m uvicorn app.main:app --host 127.0.0.1 --port 8012 --proxy-headers --forwarded-allow-ips 127.0.0.1',
      interpreter: 'none',
      instances: 1,
      exec_mode: 'fork',
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      kill_timeout: 10000,
      env: ENV,
    },
    {
      // 야간 ML 점수 (Alpha158 + LightGBM) — 평일 장 마감 후
      name: 'autostock-ml',
      cwd: CWD,
      script: PY,
      args: '-m app.ml.nightly',
      interpreter: 'none',
      cron_restart: '40 18 * * 1-5',
      autorestart: false,
      env: ENV,
    },
    {
      // 장 전 심층 리서치 (TradingAgents 구성의 멀티 에이전트) — 평일 07:20, 종목당 OpenAI 호출 6회 안팎
      name: 'autostock-research',
      cwd: CWD,
      script: PY,
      args: '-m app.research.daily',
      interpreter: 'none',
      cron_restart: '20 7 * * 1-5',
      autorestart: false,
      env: ENV,
    },
  ],
}
