import { formatAmountInput, parseAmount, won } from '../format'

export type LimitAction = 'buy' | 'all'

export interface LossLimitValue {
  limitId: string
  custom: string
  action: LimitAction
}

const PRESETS = [
  { id: 'p05', ratio: 0.005, sub: '보수적', badge: '' },
  { id: 'p15', ratio: 0.015, sub: '위험중립형 권장', badge: '추천' },
  { id: 'p25', ratio: 0.025, sub: '공격적', badge: '' },
]

const ACTIONS: { id: LimitAction; label: string; desc: string }[] = [
  { id: 'buy', label: '신규 매수만 멈추기', desc: '보유 종목의 손절·익절 주문은 계속 실행합니다' },
  { id: 'all', label: '모든 매매 멈추기', desc: '승인 대기·미체결 주문까지 취소하고, 다음 거래일에 다시 시작합니다' },
]

function roundLimit(n: number): number {
  return Math.max(10_000, Math.round(n / 10_000) * 10_000)
}

export function presetAmounts(invest: number) {
  return PRESETS.map((p) => ({ ...p, value: roundLimit(invest * p.ratio) }))
}

/** 저장된 한도 금액을 프리셋/직접 입력 상태로 바꿉니다. */
export function initialLossValue(invest: number, limit: number, action: LimitAction): LossLimitValue {
  const hit = presetAmounts(invest).find((p) => p.value === limit)
  return { limitId: hit ? hit.id : 'custom', custom: formatAmountInput(String(limit)), action }
}

export function lossAmount(invest: number, v: LossLimitValue): number {
  const p = presetAmounts(invest).find((x) => x.id === v.limitId)
  return p ? p.value : parseAmount(v.custom)
}

export function LossLimitPicker({ invest, value, onChange }: { invest: number; value: LossLimitValue; onChange: (v: LossLimitValue) => void }) {
  const presets = presetAmounts(invest)
  return (
    <>
      <div role="group" aria-label="일일 손실 한도" style={{ display: 'flex', flexWrap: 'wrap', gap: 10, marginTop: 22 }}>
        {presets.map((p) => (
          <button key={p.id} type="button" className="choice" style={{ flex: '1 1 200px' }} aria-pressed={value.limitId === p.id} onClick={() => onChange({ ...value, limitId: p.id })}>
            <span className="spread">
              <span className="mono" style={{ fontSize: 18, fontWeight: 600 }}>{won(p.value)}원</span>
              <span className="xs accent" style={{ fontWeight: 700 }}>{p.badge}</span>
            </span>
            <span className="muted" style={{ display: 'block', marginTop: 2, fontSize: 13 }}>
              투자금의 {(p.ratio * 100).toFixed(1)}% · {p.sub}
            </span>
          </button>
        ))}
        <button type="button" className="choice" style={{ flex: '1 1 200px' }} aria-pressed={value.limitId === 'custom'} onClick={() => onChange({ ...value, limitId: 'custom' })}>
          <span className="mono" style={{ fontSize: 18, fontWeight: 600 }}>직접 입력</span>
          <span className="muted" style={{ display: 'block', marginTop: 2, fontSize: 13 }}>원하는 금액을 정해요</span>
        </button>
      </div>
      {value.limitId === 'custom' && (
        <div style={{ marginTop: 14, maxWidth: 320 }}>
          <label htmlFor="custom-limit" className="field-label">직접 입력</label>
          <div className="input-box" style={{ borderColor: 'var(--accent)', marginTop: 6 }}>
            <span style={{ marginRight: 4 }}>−</span>
            <input id="custom-limit" type="text" inputMode="numeric" value={value.custom} onChange={(e) => onChange({ ...value, custom: formatAmountInput(e.target.value) })} />
            <span>원</span>
          </div>
        </div>
      )}
      <h2 style={{ margin: '28px 0 0', fontSize: 16, fontWeight: 600 }}>한도에 닿으면 AI는</h2>
      <div role="group" aria-label="한도 도달 시 동작" style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 12 }}>
        {ACTIONS.map((a) => {
          const on = a.id === value.action
          return (
            <button key={a.id} type="button" className="choice" style={{ display: 'flex', alignItems: 'flex-start', gap: 12, boxShadow: 'none' }} aria-pressed={on} onClick={() => onChange({ ...value, action: a.id })}>
              <span
                style={{
                  flex: 'none',
                  width: 18,
                  height: 18,
                  marginTop: 2,
                  borderRadius: '50%',
                  border: on ? '5px solid var(--accent)' : '2px solid var(--text-dim)',
                  background: on ? 'var(--bg)' : 'transparent',
                }}
              />
              <span>
                <span style={{ display: 'block', fontWeight: 600 }}>{a.label}</span>
                <span className="muted" style={{ display: 'block', marginTop: 2, fontSize: 13 }}>{a.desc}</span>
              </span>
            </button>
          )
        })}
      </div>
    </>
  )
}

export function LossTimeline({ amount, action }: { amount: number; action: LimitAction }) {
  const steps = [
    { pct: 50, color: 'var(--text-soft)', text: '앱 알림으로 현재 손실을 알려드려요' },
    { pct: 80, color: 'var(--warn)', text: '신규 매수 금액을 절반으로 줄여요' },
    { pct: 100, color: 'var(--up)', text: action === 'buy' ? '신규 매수를 멈추고, 보유 종목 관리만 계속해요' : '모든 매매를 멈추고 미체결 주문을 취소해요' },
  ]
  return (
    <section aria-labelledby="how-h" className="card" style={{ padding: 24, borderRadius: 16 }}>
      <h2 id="how-h">이렇게 지켜드려요</h2>
      <p className="muted" style={{ marginTop: 6, fontSize: 13 }}>오늘 손실이 쌓이면 단계별로 알리고 멈춥니다.</p>
      <div style={{ marginTop: 18 }}>
        {steps.map((s, i) => (
          <div key={s.pct} className="timeline-item">
            <div className="timeline-rail">
              <span className="node" style={{ background: s.color }} />
              {i < steps.length - 1 && <span className="line" />}
            </div>
            <div style={{ paddingBottom: i < steps.length - 1 ? 16 : 0 }}>
              <div className="mono" style={{ fontWeight: 600, color: s.pct === 50 ? undefined : s.color }}>
                −{won((amount * s.pct) / 100)}원 · {s.pct}%
              </div>
              <div className="muted" style={{ fontSize: 13 }}>{s.text}</div>
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}
