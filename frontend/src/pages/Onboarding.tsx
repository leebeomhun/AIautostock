import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, type SettingsView } from '../api'
import { Check, LogoMark } from '../components/Icons'
import { initialLossValue, lossAmount, LossLimitPicker, LossTimeline, type LossLimitValue } from '../components/LossLimit'
import { BrokerBadge } from '../components/Shell'
import { formatAmountInput, parseAmount, won } from '../format'
import { useAction } from '../hooks'

const STEPS = ['증권 계좌 연결', '투자 성향 진단', '손실 한도 정하기', '모의투자 시작', '실전 전환']
const PROFILES: { id: SettingsView['risk_profile']; label: string; desc: string; strategy: string }[] = [
  { id: 'conservative', label: '안정추구형', desc: '손실을 최소화하고 천천히 불리고 싶어요', strategy: '배당·가치 리밸런싱' },
  { id: 'neutral', label: '위험중립형', desc: '적당한 위험을 감수하고 시장보다 조금 더 벌고 싶어요', strategy: '평균회귀 스윙' },
  { id: 'aggressive', label: '적극투자형', desc: '변동이 커도 높은 수익을 노리고 싶어요', strategy: '모멘텀 돌파' },
]
const PROFILE_LABEL = Object.fromEntries(PROFILES.map((p) => [p.id, p.label]))

function Steps({ current }: { current: number }) {
  return (
    <ol aria-label="시작 단계" className="steps">
      {STEPS.map((s, i) => {
        const done = i < current
        const active = i === current
        return (
          <li key={s} aria-current={active ? 'step' : undefined}>
            <div className="bar">
              <div style={{ width: done ? '100%' : active ? '50%' : '0%' }} />
            </div>
            <div className="label" style={{ color: !done && !active ? 'var(--text-3)' : undefined }}>
              {done ? <Check color="var(--accent)" /> : `${i + 1}. `}
              {s}
            </div>
          </li>
        )
      })}
    </ol>
  )
}

export function Onboarding() {
  const navigate = useNavigate()
  const [s, setS] = useState<SettingsView | null>(null)
  const [step, setStep] = useState(0)
  const [invest, setInvest] = useState('')
  const [loss, setLoss] = useState<LossLimitValue | null>(null)
  const [agree, setAgree] = useState(false)
  const { busy, error, run } = useAction()

  useEffect(() => {
    api.settings().then((v) => {
      setS(v)
      setInvest(formatAmountInput(String(v.invest_amount)))
      setLoss(initialLossValue(v.invest_amount, v.daily_loss_limit, v.limit_action))
      setAgree(v.agreed)
      if (v.onboarded) navigate('/', { replace: true })
    })
  }, [navigate])

  if (!s || !loss) return <div className="app"><div className="empty">불러오는 중…</div></div>
  const investNum = parseAmount(invest)
  const limit = lossAmount(investNum, loss)
  const save = (patch: Partial<SettingsView>, next: number) =>
    run(() => api.saveSettings(patch)).then((v) => {
      if (v) {
        setS(v)
        setStep(next)
      }
    })

  return (
    <div className="app">
      <header className="header">
        <div className="header-inner page-narrow" style={{ justifyContent: 'space-between' }}>
          <span className="brand">
            <span className="logo"><LogoMark /></span>
            {s.app_name}
          </span>
          <div className="header-right">
            <BrokerBadge dash={s} />
          </div>
        </div>
      </header>
      <main className="page page-narrow" style={{ paddingTop: 28 }}>
        <Steps current={step} />
        {error && <div role="alert" className="banner error">{error}</div>}
        <div className="row top" style={{ gap: 24, marginTop: 8 }}>
          <section className="card" style={{ flex: '999 1 560px', padding: '28px 30px', borderRadius: 16 }} aria-labelledby="q-h">
            {step === 0 && (
              <>
                <h1 id="q-h" style={{ fontSize: 24, fontWeight: 700, letterSpacing: '-0.02em' }}>증권 계좌를 확인해 주세요</h1>
                <p className="muted" style={{ marginTop: 8 }}>
                  {s.broker.kind === 'kiwoom'
                    ? `키움증권 REST API로 연결됐습니다 (${s.broker.label}). 앱키·시크릿은 서버의 .env 에서만 읽고 화면에는 보이지 않아요.`
                    : '키움 앱키가 없어 로컬 시뮬레이션 계좌로 시작합니다. .env 에 KIWOOM_APP_KEY·KIWOOM_APP_SECRET 을 넣고 서버를 다시 켜면 키움 모의투자로 연결됩니다.'}
                </p>
                <dl className="dl" style={{ marginTop: 20, fontSize: 14 }}>
                  <div><dt>계좌</dt><dd>{s.account_label ?? '조회 실패'}</dd></div>
                  <div><dt>평가자산</dt><dd className="mono">{won(s.equity)}원</dd></div>
                  <div><dt>주문 가능 현금</dt><dd className="mono">{won(s.cash)}원</dd></div>
                  <div><dt>AI 판단 모델</dt><dd className="mono">{s.ai.model}</dd></div>
                </dl>
                {s.error && <div className="hint error">{s.error}</div>}
                <div className="spread" style={{ marginTop: 24, justifyContent: 'flex-end' }}>
                  <button type="button" className="btn btn-primary btn-lg" disabled={!s.account_label} onClick={() => setStep(1)}>다음: 투자 성향</button>
                </div>
              </>
            )}
            {step === 1 && (
              <>
                <h1 id="q-h" style={{ fontSize: 24, fontWeight: 700, letterSpacing: '-0.02em' }}>어떤 투자를 원하세요?</h1>
                <p className="muted" style={{ marginTop: 8 }}>고른 성향에 맞는 추천 전략 하나로 모의투자를 시작해요. 나중에 전략은 얼마든지 더 만들 수 있어요.</p>
                <div role="group" aria-label="투자 성향" className="stack" style={{ gap: 10, marginTop: 22 }}>
                  {PROFILES.map((p) => (
                    <button key={p.id} type="button" className="choice" aria-pressed={s.risk_profile === p.id} onClick={() => setS({ ...s, risk_profile: p.id })}>
                      <span className="spread"><span style={{ fontWeight: 600 }}>{p.label}</span><span className="small dim">추천 전략 · {p.strategy}</span></span>
                      <span className="muted" style={{ display: 'block', marginTop: 2, fontSize: 13 }}>{p.desc}</span>
                    </button>
                  ))}
                </div>
                <div style={{ marginTop: 22, maxWidth: 320 }}>
                  <label htmlFor="invest" className="field-label">AI에게 맡길 투자 예정 금액</label>
                  <div className="input-box">
                    <input id="invest" type="text" inputMode="numeric" value={invest} onChange={(e) => setInvest(formatAmountInput(e.target.value))} />
                    <span>원</span>
                  </div>
                  <div className={`hint ${s.equity != null && investNum > s.equity ? 'error' : ''}`}>
                    {s.equity != null && investNum > s.equity ? '평가자산보다 큽니다' : `평가자산 ${won(s.equity)}원`}
                  </div>
                </div>
                <div className="spread wrap" style={{ marginTop: 24 }}>
                  <button type="button" className="btn btn-ghost btn-lg" onClick={() => setStep(0)}>이전</button>
                  <button
                    type="button"
                    className="btn btn-primary btn-lg"
                    disabled={busy || investNum <= 0 || (s.equity != null && investNum > s.equity)}
                    onClick={() => {
                      setLoss(initialLossValue(investNum, Math.round(investNum * 0.015 / 10_000) * 10_000, loss.action))
                      save({ risk_profile: s.risk_profile, invest_amount: investNum }, 2)
                    }}
                  >
                    다음: 손실 한도
                  </button>
                </div>
              </>
            )}
            {step === 2 && (
              <>
                <h1 id="q-h" style={{ fontSize: 24, fontWeight: 700, letterSpacing: '-0.02em' }}>하루에 감당할 수 있는 최대 손실은 얼마인가요?</h1>
                <p className="muted" style={{ marginTop: 8 }}>이 금액에 닿으면 AI가 그날 매매를 멈춥니다. 투자 예정 금액 {won(investNum)}원 기준이며, 언제든 설정에서 바꿀 수 있어요.</p>
                <LossLimitPicker invest={investNum} value={loss} onChange={setLoss} />
                <label style={{ display: 'flex', alignItems: 'flex-start', gap: 12, marginTop: 24, padding: '14px 16px', borderRadius: 12, background: 'var(--surface-1)', border: '1px solid var(--border)', cursor: 'pointer' }}>
                  <input type="checkbox" checked={agree} onChange={() => setAgree(!agree)} style={{ marginTop: 1 }} />
                  <span>AI 매매도 손실이 날 수 있고, 투자 결과의 책임은 나에게 있다는 것을 이해했습니다.</span>
                </label>
                <div className="spread wrap" style={{ marginTop: 24 }}>
                  <button type="button" className="btn btn-ghost btn-lg" onClick={() => setStep(1)}>이전</button>
                  <button
                    type="button"
                    className="btn btn-primary btn-lg"
                    aria-disabled={!agree || limit <= 0}
                    disabled={busy || !agree || limit <= 0}
                    onClick={() => save({ daily_loss_limit: limit, limit_action: loss.action, agreed: agree }, 3)}
                  >
                    다음: 모의투자 시작
                  </button>
                </div>
              </>
            )}
            {step >= 3 && (
              <>
                <h1 id="q-h" style={{ fontSize: 24, fontWeight: 700, letterSpacing: '-0.02em' }}>모의투자로 시작해요</h1>
                <p className="muted" style={{ marginTop: 8 }}>
                  추천 전략 하나를 <strong style={{ color: 'var(--text)' }}>승인 후 주문</strong> 모드로 켭니다. AI가 주문을 제안하면 근거를 보고 직접 승인해 주세요.
                  실전 전환은 {s.paper_days_required}일 운영 후 열립니다.
                </p>
                <dl className="dl" style={{ marginTop: 20, fontSize: 14 }}>
                  <div><dt>추천 전략</dt><dd>{PROFILES.find((p) => p.id === s.risk_profile)?.strategy} · 코스피200</dd></div>
                  <div><dt>맡길 금액</dt><dd className="mono">{won(s.invest_amount)}원</dd></div>
                  <div><dt>손절 · 익절 · 종목 비중</dt><dd>−5% · +10% · 15%</dd></div>
                  <div><dt>일일 손실 한도</dt><dd className="mono">−{won(s.daily_loss_limit)}원</dd></div>
                </dl>
                <div className="spread wrap" style={{ marginTop: 24 }}>
                  <button type="button" className="btn btn-ghost btn-lg" onClick={() => setStep(2)}>이전</button>
                  <button
                    type="button"
                    className="btn btn-primary btn-lg"
                    disabled={busy}
                    onClick={() => run(() => api.completeOnboarding()).then((v) => v && navigate('/', { replace: true }))}
                  >
                    모의투자 시작하기
                  </button>
                </div>
              </>
            )}
          </section>

          <aside aria-label="설명과 요약" className="stack" style={{ flex: '1 1 340px', minWidth: 0, gap: 16 }}>
            <LossTimeline amount={limit} action={loss.action} />
            <section aria-labelledby="sum-h" className="card" style={{ padding: 24, borderRadius: 16 }}>
              <h2 id="sum-h">지금까지 설정</h2>
              <dl className="dl">
                <div><dt>연결 계좌</dt><dd>{s.account_label ?? '—'} {!s.broker.is_live && <span className="warn">({s.broker.kind === 'kiwoom' ? '모의' : '로컬'})</span>}</dd></div>
                <div><dt>투자 성향</dt><dd>{PROFILE_LABEL[s.risk_profile]}</dd></div>
                <div><dt>투자 예정 금액</dt><dd className="mono">{won(investNum)}원</dd></div>
              </dl>
              {step > 0 && (
                <button type="button" className="btn-link" style={{ paddingLeft: 0 }} onClick={() => setStep(1)}>수정하기 →</button>
              )}
            </section>
          </aside>
        </div>
      </main>
    </div>
  )
}
