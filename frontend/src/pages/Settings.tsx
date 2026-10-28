import { useEffect, useState } from 'react'
import { api, type SettingsView } from '../api'
import { initialLossValue, lossAmount, LossLimitPicker, LossTimeline, type LossLimitValue } from '../components/LossLimit'
import { PageError, useShell } from '../components/Shell'
import { won } from '../format'
import { useAction, usePolling } from '../hooks'

interface StrategyItem {
  id: number
  name: string
  template: string
  version: string
  allocation: number
  stop_pct: number
  take_pct: number
  max_weight_pct: number
  exec_mode: string
  status: 'running' | 'paused' | 'stopped'
  pause_reason: string | null
}

const EXEC_LABEL: Record<string, string> = { notify: '알림만', approve: '승인 후 주문', auto: '완전 자동' }
const STATUS_LABEL: Record<string, string> = { running: '실행 중', paused: '일시정지', stopped: '중지' }

function Strategies() {
  const { refresh: refreshShell } = useShell()
  const { data, refresh } = usePolling(() => fetch('/api/strategies').then((r) => r.json() as Promise<StrategyItem[]>), 15000)
  const { busy, error, run } = useAction()
  const set = (id: number, status: string) => run(() => api.patchStrategy(id, status)).then(() => { refresh(); refreshShell() })
  return (
    <section className="card" aria-labelledby="st-h">
      <h2 id="st-h">전략 관리</h2>
      <p className="card-sub">일시정지하면 신규 진입만 멈추고, 보유 종목의 손절·익절은 계속 관리합니다.</p>
      {error && <div className="hint error">{error}</div>}
      <div className="stack" style={{ gap: 10, marginTop: 14 }}>
        {(data ?? []).filter((s) => s.status !== 'stopped').map((s) => (
          <div key={s.id} className="inner spread wrap" style={{ gap: 12 }}>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontWeight: 600 }}>{s.name} <span className="dim mono small">{s.version}</span></div>
              <div className="muted" style={{ fontSize: 13 }}>
                {won(s.allocation)}원 · 손절 −{s.stop_pct}% · 익절 +{s.take_pct}% · 비중 {s.max_weight_pct}% · {EXEC_LABEL[s.exec_mode]}
              </div>
              {s.pause_reason && <div className="hint warn">{s.pause_reason}</div>}
            </div>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <span className={`pill ${s.status === 'running' ? 'good' : 'warn'}`}>{STATUS_LABEL[s.status]}</span>
              {s.status === 'running' ? (
                <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => set(s.id, 'paused')}>일시정지</button>
              ) : (
                <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => set(s.id, 'running')}>다시 실행</button>
              )}
              <button
                type="button"
                className="btn btn-ghost"
                disabled={busy}
                onClick={() => window.confirm(`${s.name} 전략을 중지할까요? 보유 종목은 남지만 AI가 더 이상 관리하지 않습니다.`) && set(s.id, 'stopped')}
              >
                중지
              </button>
            </div>
          </div>
        ))}
        {data && data.filter((s) => s.status !== 'stopped').length === 0 && <div className="empty">운영 중인 전략이 없습니다.</div>}
      </div>
    </section>
  )
}

export function Settings() {
  const { refresh } = useShell()
  const [s, setS] = useState<SettingsView | null>(null)
  const [loss, setLoss] = useState<LossLimitValue | null>(null)
  const [saved, setSaved] = useState(false)
  const { busy, error, run } = useAction()
  useEffect(() => {
    api.settings().then((v) => {
      setS(v)
      setLoss(initialLossValue(v.invest_amount, v.daily_loss_limit, v.limit_action))
    })
  }, [])
  if (!s || !loss) return <main className="page"><div className="empty">불러오는 중…</div></main>
  const limit = lossAmount(s.invest_amount, loss)
  return (
    <main className="page page-narrow">
      <div className="page-title"><h1>설정</h1></div>
      <PageError message={error ?? s.error} />
      <div className="row top" style={{ gap: 24 }}>
        <div className="col-main">
          <section className="card" aria-labelledby="loss-h" style={{ padding: '24px 26px' }}>
            <h2 id="loss-h" style={{ fontSize: 18 }}>일일 손실 한도</h2>
            <p className="card-sub">투자 예정 금액 {won(s.invest_amount)}원 기준입니다.</p>
            <LossLimitPicker invest={s.invest_amount} value={loss} onChange={(v) => { setLoss(v); setSaved(false) }} />
            <div className="spread" style={{ marginTop: 20, justifyContent: 'flex-end', gap: 12 }}>
              {saved && <span className="small accent" role="status">저장했습니다</span>}
              <button
                type="button"
                className="btn btn-primary btn-lg"
                disabled={busy || limit <= 0}
                onClick={() => run(() => api.saveSettings({ daily_loss_limit: limit, limit_action: loss.action })).then((v) => { if (v) { setS(v); setSaved(true); refresh() } })}
              >
                저장
              </button>
            </div>
          </section>
          <Strategies />
        </div>
        <aside className="col-side">
          <LossTimeline amount={limit} action={loss.action} />
          <section className="card" aria-labelledby="conn-h">
            <h2 id="conn-h">연결 상태</h2>
            <dl className="dl">
              <div><dt>증권 계좌</dt><dd>{s.account_label ?? '—'}</dd></div>
              <div><dt>주문 경로</dt><dd>{s.broker.kind === 'kiwoom' ? `키움 REST · ${s.broker.label}` : '로컬 시뮬레이션'}</dd></div>
              <div><dt>시세</dt><dd>{s.market_source === 'kiwoom' ? '키움 REST' : '모의 시세'}</dd></div>
              <div><dt>AI 모델</dt><dd className="mono">{s.ai.model}</dd></div>
              <div><dt>모의투자 운영</dt><dd>{s.paper_days}일 / {s.paper_days_required}일</dd></div>
            </dl>
          </section>
          <section className="card strong" aria-labelledby="live-h">
            <h2 id="live-h">실전 주문 허용</h2>
            {s.broker.is_live ? (
              s.live_confirmed ? (
                <p className="card-sub">실전 계좌로 주문하고 있습니다. 대시보드의 긴급 정지로 언제든 멈출 수 있어요.</p>
              ) : (
                <>
                  <p className="card-sub">실전 계좌가 연결되어 있지만 지금은 알림만 보냅니다. {s.paper_days_required}일 운영 후 실전 주문을 허용할 수 있어요.</p>
                  <button
                    type="button"
                    className="btn btn-danger btn-block"
                    style={{ marginTop: 14 }}
                    disabled={busy || !s.live_unlocked}
                    onClick={() => window.confirm('실전 계좌로 실제 주문을 내도록 허용할까요? 손실이 날 수 있습니다.') && run(() => api.confirmLive()).then((v) => { if (v) { setS(v); refresh() } })}
                  >
                    실전 주문 허용하기
                  </button>
                </>
              )
            ) : (
              <p className="card-sub">
                지금은 {s.broker.label} 계좌입니다. 실전 전환은 .env 에 실전 앱키를 넣고 KIWOOM_MODE=real 로 서버를 다시 켠 뒤, 여기서 한 번 더 확인해야 열립니다.
              </p>
            )}
          </section>
        </aside>
      </div>
    </main>
  )
}
