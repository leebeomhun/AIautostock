import { useEffect, useRef } from 'react'
import { Link } from 'react-router-dom'
import { api, type PendingDecision } from '../api'
import { mmss, sideLabel, signedWon, won } from '../format'
import { useAction, useNow } from '../hooks'
import { useShell } from './Shell'

/** 모바일 승인 요청 바텀시트 (디자인 ⑤). */
export function ApprovalSheet({ decision, onClose, onDone }: { decision: PendingDecision; onClose: () => void; onDone: () => void }) {
  const { toast } = useShell()
  const { busy, error, run } = useAction()
  const now = useNow()
  const dialogRef = useRef<HTMLElement>(null)
  const left = decision.expires_at ? (new Date(decision.expires_at).getTime() - now) / 1000 : null
  const side = sideLabel(decision.side)

  useEffect(() => {
    dialogRef.current?.focus()
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    dialogRef.current?.addEventListener('keydown', onKey)
    const el = dialogRef.current
    return () => el?.removeEventListener('keydown', onKey)
  }, [onClose])

  const approve = () =>
    run(() => api.approve(decision.id)).then((r) => {
      if (!r) return
      onDone()
      toast(`${side} 주문을 보냈어요`, { label: '주문 취소', onClick: () => api.cancelOrder(decision.id).then(onDone, (e) => toast(e.message)) })
    })
  const reject = () =>
    run(() => api.reject(decision.id)).then((r) => {
      if (!r) return
      onDone()
      toast(decision.side === 'sell' ? '거절했어요 · 계속 보유합니다' : '거절했어요 · 매수하지 않습니다', {
        label: '되돌리기',
        onClick: () => api.reopen(decision.id).then(onDone, (e) => toast(e.message)),
      })
    })

  return (
    <>
      <div className="sheet-backdrop" aria-hidden="true" onClick={onClose} />
      <section ref={dialogRef} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby="sheet-h" className="sheet">
        <div className="sheet-grip" />
        <div className="spread">
          <span className="pill warn">승인 요청</span>
          {left != null && <span className="mono warn" style={{ fontSize: 13 }}>{mmss(left)} 후 자동 취소</span>}
        </div>
        <h2 id="sheet-h" style={{ marginTop: 12, fontSize: 22, fontWeight: 700, letterSpacing: '-0.02em' }}>
          {decision.name} {decision.qty}주 <span className={decision.side === 'buy' ? 'up' : 'down'}>{side}</span>
        </h2>
        <div className="muted" style={{ fontSize: 13 }}>
          {decision.strategy} · 시장가 · 신뢰도 {decision.confidence}%
        </div>
        <p style={{ marginTop: 14, padding: '12px 14px', borderRadius: 10, background: 'var(--surface-1)', lineHeight: 1.55 }}>{decision.summary}</p>
        <div className="row" style={{ gap: 10, marginTop: 12 }}>
          <div className="inner" style={{ flex: 1, padding: '10px 12px', background: 'transparent' }}>
            <div className="small muted">예상 금액</div>
            <div className="mono" style={{ fontWeight: 600 }}>{won(decision.amount)}원</div>
          </div>
          {decision.pnl != null && (
            <div className="inner" style={{ flex: 1, padding: '10px 12px', background: 'transparent' }}>
              <div className="small muted">예상 실현손익</div>
              <div className={`mono ${decision.pnl >= 0 ? 'up' : 'down'}`} style={{ fontWeight: 600 }}>{signedWon(decision.pnl)}원</div>
            </div>
          )}
        </div>
        {error && <div className="hint error">{error}</div>}
        <button type="button" className="btn btn-primary btn-block" style={{ minHeight: 54, marginTop: 16, fontSize: 16 }} disabled={busy} onClick={approve}>
          승인하고 {side}
        </button>
        <div className="row" style={{ gap: 8, marginTop: 8 }}>
          <button type="button" className="btn btn-ghost" style={{ flex: 1, minHeight: 48 }} disabled={busy} onClick={reject}>
            거절
          </button>
          <Link to={`/decisions/${decision.id}`} className="btn btn-soft" style={{ flex: 1, minHeight: 48 }}>
            근거 자세히
          </Link>
        </div>
      </section>
    </>
  )
}
