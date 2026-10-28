import { api, type Dashboard, type Mode } from '../api'
import { won } from '../format'
import { useAction } from '../hooks'
import { Power } from './Icons'

const MODES: { id: Mode; label: string }[] = [
  { id: 'notify', label: '알림만' },
  { id: 'approve', label: '승인 후 주문' },
  { id: 'auto', label: '완전 자동' },
]

function subtitle(d: Dashboard): string {
  const st = d.status!
  if (!st.running) {
    return st.cancelled_on_stop
      ? `새 주문을 내지 않습니다 · 승인 대기·미체결 ${st.cancelled_on_stop}건을 취소했습니다`
      : '새 주문을 내지 않습니다'
  }
  const counts =
    st.strategies_paused > 0
      ? `전략 ${st.strategies_running}개 실행, ${st.strategies_paused}개 일시정지`
      : `전략 ${st.strategies_running}개 실행`
  if (st.live_locked) return `실전 계좌 · 실전 주문 허용 전까지 알림만 보냅니다 · ${counts}`
  if (st.mode === 'notify') return `AI는 매매 신호만 알려주고 주문하지 않습니다 · ${counts}`
  if (st.mode === 'approve') return `AI가 제안한 주문은 내가 승인해야 실행됩니다 · ${counts}`
  return `설정한 한도 안에서 AI가 바로 주문합니다 · ${counts}`
}

export function StatusPanel({ dash, onChange }: { dash: Dashboard; onChange: () => void }) {
  const { busy, error, run } = useAction()
  const st = dash.status!
  const loss = dash.loss!
  const running = st.running
  const lossColor = loss.pct >= 100 ? 'var(--up)' : loss.pct >= 80 ? 'var(--warn)' : 'var(--accent)'
  return (
    <section aria-label="AI 실행 상태" className="card status-panel">
      <div className="status-main">
        <span
          className="status-dot"
          style={{
            background: running ? 'var(--accent)' : 'var(--up)',
            boxShadow: `0 0 0 5px ${running ? 'rgba(200, 240, 74, 0.18)' : 'rgba(255, 92, 92, 0.18)'}`,
          }}
        />
        <div style={{ minWidth: 0 }}>
          <div className="status-title">{running ? 'AI 자동매매 실행 중' : 'AI 자동매매 정지됨'}</div>
          <div className="muted" style={{ fontSize: 13 }}>{subtitle(dash)}</div>
          {error && <div className="hint error">{error}</div>}
        </div>
      </div>
      <div role="group" aria-label="자동화 모드" className="segmented hide-sm-flex">
        {MODES.map((m) => (
          <button
            key={m.id}
            type="button"
            aria-pressed={st.mode === m.id}
            disabled={busy}
            onClick={() => st.mode !== m.id && run(() => api.setMode(m.id)).then(onChange)}
          >
            {m.label}
          </button>
        ))}
      </div>
      <div className="loss-box">
        <div className="spread small" style={{ fontSize: 13 }}>
          <span className="muted">일일 손실 한도</span>
          <span className="mono">
            {loss.used ? '−' : ''}{won(loss.used)} / −{won(loss.limit)}원
          </span>
        </div>
        <div className="meter" style={{ marginTop: 8 }}>
          <div style={{ width: `${Math.min(100, loss.pct)}%`, background: lossColor }} />
        </div>
        <div className="hint">
          {loss.pct}% 사용 · 한도에 닿으면 {loss.action === 'buy' ? '신규 매수 자동 중지' : '모든 매매 중지'}
        </div>
      </div>
      <button
        type="button"
        className={`btn btn-lg ${running ? 'btn-danger' : 'btn-primary'}`}
        disabled={busy}
        onClick={() => run<unknown>(() => (running ? api.stop() : api.start())).then(onChange)}
      >
        <Power />
        <span>{running ? '긴급 정지' : '다시 시작'}</span>
      </button>
    </section>
  )
}
