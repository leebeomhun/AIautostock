import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, type BacktestResult, type Mode, type StrategiesMeta, type TemplateDef, type UniverseDef } from '../api'
import { LineChart } from '../components/Charts'
import { PageError, useShell } from '../components/Shell'
import { formatAmountInput, parseAmount, signedPct, won } from '../format'
import { useAction } from '../hooks'

const RISK_CLASS: Record<TemplateDef['risk'], string> = { 높음: 'warn', 중간: '', 낮음: 'accent' }
const EXECS: { id: Mode; label: string; badge?: string; desc: string }[] = [
  { id: 'notify', label: '알림만', desc: '신호만 받고 주문은 직접 냅니다' },
  { id: 'approve', label: '승인 후 주문', badge: '실거래 기본', desc: 'AI가 주문을 제안하면 내가 승인합니다' },
  { id: 'auto', label: '완전 자동', badge: '모의 기본', desc: '한도 안에서 AI가 바로 주문합니다' },
]

function Section({ id, n, title, children, sub }: { id: string; n: number; title: string; sub?: string; children: React.ReactNode }) {
  return (
    <section id={id} aria-labelledby={`${id}-h`} className="card" style={{ padding: '22px 24px' }}>
      <h2 id={`${id}-h`} style={{ fontSize: 16 }}>
        <span className="step-num">{n}</span>
        {title}
      </h2>
      {sub && <p className="muted" style={{ marginTop: 6, fontSize: 13 }}>{sub}</p>}
      {children}
    </section>
  )
}

function Slider({ id, label, value, min, max, onChange, display, color, hint }: { id: string; label: string; value: number; min: number; max: number; onChange: (n: number) => void; display: string; color?: string; hint: string }) {
  return (
    <div className="inner" style={{ flex: '1 1 260px', padding: 16, borderRadius: 12 }}>
      <div className="spread base">
        <label htmlFor={id} style={{ fontWeight: 600 }}>{label}</label>
        <span className="mono" style={{ fontSize: 18, fontWeight: 600, color }}>{display}</span>
      </div>
      <input id={id} type="range" min={min} max={max} step={1} value={value} onChange={(e) => onChange(Number(e.target.value))} />
      <div className="hint" style={{ marginTop: 0 }}>{hint}</div>
    </div>
  )
}

function AmountField({ id, label, value, onChange, prefix, unit, hint }: { id: string; label: string; value: string; onChange: (v: string) => void; prefix?: string; unit: string; hint: string }) {
  return (
    <div style={{ flex: '1 1 260px' }}>
      <label htmlFor={id} className="field-label">{label}</label>
      <div className="input-box">
        {prefix && <span style={{ marginRight: 4 }}>{prefix}</span>}
        <input id={id} type="text" inputMode="numeric" value={value} onChange={(e) => onChange(formatAmountInput(e.target.value))} />
        <span>{unit}</span>
      </div>
      <div className="hint">{hint}</div>
    </div>
  )
}

function Backtest({ result, loading, name, stop, weight }: { result: BacktestResult | null; loading: boolean; name: string; stop: number; weight: number }) {
  const period = result?.start ? `${result.start.slice(0, 7).replace('-', '.')}–${result.end!.slice(0, 7).replace('-', '.')}` : ''
  return (
    <section aria-labelledby="bt-h" className="card" style={{ padding: 22 }}>
      <div className="spread base">
        <h2 id="bt-h">백테스트 미리보기</h2>
        <span className="small dim mono">{loading ? '계산 중…' : period}</span>
      </div>
      <div className="muted" style={{ marginTop: 4, fontSize: 13 }}>
        {name} · 손절 −{stop}% · 비중 {weight}%
      </div>
      {result && !result.ok && <div className="notice" style={{ marginTop: 14 }}>{result.message}</div>}
      {result?.ok && (
        <>
          <div style={{ marginTop: 14 }}>
            <LineChart
              height={130}
              grid={false}
              label={`${name} 백테스트: 연평균 ${signedPct(result.cagr, 1)}, 최대 낙폭 ${signedPct(result.mdd, 1)}`}
              series={[
                { values: fillGaps(result.curve.map((c) => c.benchmark)), color: '#6B7383', width: 1.8 },
                { values: fillGaps(result.curve.map((c) => c.strategy)), color: '#C8F04A', width: 2.4 },
              ]}
            />
          </div>
          <div style={{ display: 'flex', gap: 14, marginTop: 6, fontSize: 12 }} className="muted">
            <span className="legend-item"><span className="swatch" style={{ width: 14 }} />이 전략</span>
            <span className="legend-item"><span className="swatch bench" style={{ width: 14 }} />KOSPI</span>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 10, marginTop: 16 }}>
            {[
              { label: '연평균 수익률', value: signedPct(result.cagr, 1), cls: result.cagr == null ? '' : result.cagr >= 0 ? 'up' : 'down' },
              { label: '최대 낙폭 (MDD)', value: signedPct(result.mdd, 1), cls: 'down' },
              { label: '승률', value: result.win_rate == null ? '—' : `${result.win_rate}%`, cls: '' },
              { label: '총 거래 횟수', value: `${result.trades.toLocaleString('ko-KR')}회`, cls: '' },
            ].map((m) => (
              <div key={m.label} style={{ padding: 12, borderRadius: 10, background: 'var(--surface-1)' }}>
                <div className="small muted">{m.label}</div>
                <div className={`mono ${m.cls}`} style={{ marginTop: 2, fontSize: 18, fontWeight: 600 }}>{m.value}</div>
              </div>
            ))}
          </div>
          <div className="notice" style={{ marginTop: 14 }}>
            {result.worst_month && `가장 나빴던 달은 ${result.worst_month.label}(${signedPct(result.worst_month.ret, 1)})이었습니다. `}
            {result.note && `${result.note} `}
            과거 결과는 미래 수익을 보장하지 않으며, 수수료·슬리피지를 반영한 추정치입니다.
            {result.data_source === 'simulated' && ' 지금은 키움 키가 없어 모의 시세로 계산했습니다.'}
          </div>
          {result.warnings?.length > 0 && (
            <ul className="small muted" style={{ marginTop: 10, paddingLeft: 18, display: 'grid', gap: 4 }} aria-label="백테스트 참고 사항">
              {result.warnings.map((w) => <li key={w}>{w}</li>)}
            </ul>
          )}
        </>
      )}
    </section>
  )
}

/** 백테스트 곡선의 빈 값(null)은 앞 값으로 채우고, 앞부분 빈 값은 첫 값으로 채웁니다. */
function fillGaps(values: (number | null)[]): number[] {
  const first = values.find((v) => v != null)
  if (first == null) return []
  let last = first
  return values.map((v) => (last = v ?? last))
}

export function StrategyNew() {
  const navigate = useNavigate()
  const { refresh, toast } = useShell()
  const [meta, setMeta] = useState<StrategiesMeta | null>(null)
  const [metaError, setMetaError] = useState<string | null>(null)
  const [strat, setStrat] = useState<TemplateDef['id']>('meanrev')
  const [uni, setUni] = useState<UniverseDef['id']>('k200')
  const [custom, setCustom] = useState<string[]>([])
  const [excl, setExcl] = useState(true)
  const [amount, setAmount] = useState('')
  const [stop, setStop] = useState(5)
  const [take, setTake] = useState(10)
  const [weight, setWeight] = useState(15)
  const [loss, setLoss] = useState('300,000')
  const [orders, setOrders] = useState('20')
  // 모의투자는 완전 자동, 실거래는 승인 후 주문이 기본입니다 (불러온 뒤 계좌 종류로 정함).
  const [exec, setExec] = useState<Mode>('approve')
  const [bt, setBt] = useState<BacktestResult | null>(null)
  const [btLoading, setBtLoading] = useState(false)
  const create = useAction()

  useEffect(() => {
    api.strategiesMeta().then(
      (m) => {
        setMeta(m)
        setExec(m.broker.is_live ? 'approve' : 'auto')
        setAmount(formatAmountInput(String(Math.min(20_000_000, Math.floor(m.cash ?? 0)))))
        setLoss(formatAmountInput(String(m.defaults.daily_loss_limit)))
      },
      (e) => setMetaError(e.message),
    )
  }, [])

  const amountNum = parseAmount(amount)
  const body = useMemo(
    () => ({
      template: strat,
      universe: uni,
      custom_symbols: custom,
      amount: amountNum,
      stop_pct: stop,
      take_pct: take,
      max_weight_pct: weight,
      max_orders_per_day: Math.max(1, parseAmount(orders)),
      daily_loss_limit: parseAmount(loss),
      exclude_risky: excl,
      exec_mode: exec,
    }),
    [strat, uni, custom, amountNum, stop, take, weight, orders, loss, excl, exec],
  )

  // 조건을 바꾸면 잠시 뒤 백테스트를 다시 계산합니다.
  useEffect(() => {
    if (!meta || amountNum <= 0 || (uni === 'custom' && custom.length === 0)) return
    const t = window.setTimeout(() => {
      setBtLoading(true)
      api
        .backtest(body)
        .then(setBt, (e) => setBt({ ok: false, message: e.message } as BacktestResult))
        .finally(() => setBtLoading(false))
    }, 400)
    return () => window.clearTimeout(t)
  }, [meta, strat, uni, custom, amountNum, stop, take, weight, orders]) // eslint-disable-line react-hooks/exhaustive-deps

  if (metaError) return <main className="page"><PageError message={metaError} /></main>
  if (!meta) return <main className="page"><div className="empty">불러오는 중…</div></main>

  const tpl = meta.templates.find((t) => t.id === strat)!
  const uniDef = meta.universes.find((u) => u.id === uni)!
  const execDef = EXECS.find((x) => x.id === exec)!
  const amountError = meta.equity != null && amountNum > meta.equity ? '맡길 금액이 계좌 평가자산보다 큽니다' : amountNum <= 0 ? '금액을 입력해 주세요' : null
  const isLive = meta.broker.is_live
  const start = () =>
    create.run(() => api.createStrategy(body)).then((r) => {
      if (!r) return
      refresh()
      toast(`${tpl.name} 전략을 시작했습니다`)
      navigate('/')
    })

  return (
    <main className="page" style={{ paddingBottom: 40 }}>
      <div className="spread wrap" style={{ alignItems: 'flex-end', gap: '12px 24px' }}>
        <div>
          <h1 style={{ fontSize: 26, fontWeight: 700, letterSpacing: '-0.02em' }}>새 전략 만들기</h1>
          <p className="muted" style={{ marginTop: 6 }}>전략을 고르고 한도를 정하면, 실행 전에 과거 데이터로 어떻게 움직였을지 바로 보여드려요.</p>
        </div>
        <nav aria-label="설정 단계" className="step-nav">
          {['전략', '투자 대상', '리스크 한도', '실행 방식'].map((s, i) => (
            <a key={s} href={`#s${i + 1}`}>
              <span className="mono accent">{i + 1}</span>
              {s}
            </a>
          ))}
        </nav>
      </div>

      <div className="row top" style={{ gap: 20 }}>
        <div className="col-main" style={{ flexBasis: 640 }}>
          <Section id="s1" n={1} title="어떤 방식으로 매매할까요?">
            <div role="group" aria-label="전략 템플릿" className="row" style={{ gap: 12, marginTop: 16 }}>
              {meta.templates.map((t) => (
                <button key={t.id} type="button" className="choice" style={{ flex: '1 1 260px', padding: '16px 18px' }} aria-pressed={t.id === strat} disabled={!t.available} onClick={() => setStrat(t.id)}>
                  <span className="spread">
                    <span style={{ fontSize: 15, fontWeight: 600 }}>{t.name}</span>
                    <span className={`small ${RISK_CLASS[t.risk]}`} style={{ fontWeight: 600, color: t.risk === '중간' ? 'var(--text-soft)' : undefined }}>위험도 {t.risk}</span>
                  </span>
                  <span className="muted" style={{ display: 'block', marginTop: 6, fontSize: 13 }}>{t.desc}</span>
                  {!t.available && <span className="hint" style={{ display: 'block' }}>{t.note ?? '지금은 쓸 수 없습니다'}</span>}
                  <span style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 12 }}>
                    <span className="pill">보유 {t.hold}</span>
                    <span className="pill">매매 {t.freq}</span>
                  </span>
                </button>
              ))}
            </div>
          </Section>

          <Section id="s2" n={2} title="어디에 얼마를 투자할까요?">
            <div className="row" style={{ gap: '20px 32px', marginTop: 16 }}>
              <div style={{ flex: '2 1 340px' }}>
                <div className="field-label">종목 범위</div>
                <div role="group" aria-label="종목 범위" style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 8 }}>
                  {meta.universes.map((u) => (
                    <button key={u.id} type="button" className="chip-btn" aria-pressed={u.id === uni} disabled={!u.available} title={u.note ?? undefined} onClick={() => setUni(u.id)}>
                      {u.label}
                    </button>
                  ))}
                </div>
                {meta.universes.some((u) => !u.available) && <div className="hint">미국 S&P500은 키움 계좌에서 주문할 수 없어 로컬 시뮬레이션에서만 고를 수 있습니다.</div>}
                {uni === 'custom' && (
                  <fieldset style={{ border: '1px solid var(--border)', borderRadius: 10, marginTop: 12, padding: '10px 12px', maxHeight: 220, overflowY: 'auto' }}>
                    <legend className="small muted">종목 고르기 · {custom.length}개 선택</legend>
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 4 }}>
                      {meta.pickable_symbols.map((s) => (
                        <label key={s.symbol} style={{ display: 'flex', alignItems: 'center', gap: 8, minHeight: 36, fontSize: 13, cursor: 'pointer' }}>
                          <input
                            type="checkbox"
                            checked={custom.includes(s.symbol)}
                            onChange={() => setCustom((c) => (c.includes(s.symbol) ? c.filter((x) => x !== s.symbol) : [...c, s.symbol]))}
                            style={{ width: 18, height: 18 }}
                          />
                          {s.name}
                        </label>
                      ))}
                    </div>
                  </fieldset>
                )}
                <label style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 14, minHeight: 44, fontSize: 13, color: 'var(--text-soft)', cursor: 'pointer' }}>
                  <input type="checkbox" checked={excl} onChange={() => setExcl(!excl)} style={{ width: 18, height: 18 }} />
                  관리종목·거래정지·투자경고 종목은 자동으로 제외
                </label>
              </div>
              <div style={{ flex: '1 1 220px' }}>
                <label htmlFor="amt" className="field-label">이 전략에 맡길 금액</label>
                <div className="input-box">
                  <input id="amt" type="text" inputMode="numeric" value={amount} onChange={(e) => setAmount(formatAmountInput(e.target.value))} aria-invalid={!!amountError} />
                  <span>원</span>
                </div>
                <div className={`hint ${amountError ? 'error' : ''}`}>{amountError ?? `주문 가능 현금 ${won(meta.cash)}원 · ${meta.broker.label}`}</div>
                {!amountError && meta.equity != null && meta.allocated + amountNum > meta.equity && (
                  <div className="hint warn">
                    다른 전략에 맡긴 {won(meta.allocated)}원과 합치면 평가자산을 넘습니다. 전략끼리 현금을 나눠 쓰므로 먼저 낸 주문이 현금을 씁니다.
                  </div>
                )}
              </div>
            </div>
          </Section>

          <Section id="s3" n={3} title="얼마까지 잃어도 괜찮을까요?" sub="AI는 아래 한도를 넘지 않도록 주문 직전에 다시 확인합니다. 한도는 언제든 바꿀 수 있어요.">
            <div className="row" style={{ marginTop: 18 }}>
              <Slider id="stop" label="손절 기준" value={stop} min={2} max={15} onChange={setStop} display={`−${stop}%`} color="var(--down)" hint={`평균단가보다 ${stop}% 내려가면 매도합니다`} />
              <Slider id="take" label="익절 기준" value={take} min={3} max={30} onChange={setTake} display={`+${take}%`} color="var(--up)" hint={`${take}% 오르면 절반을 팔아 수익을 확정합니다`} />
              <Slider id="weight" label="종목당 최대 비중" value={weight} min={5} max={30} onChange={setWeight} display={`${weight}%`} hint={`한 종목에 최대 ${won((amountNum * weight) / 100)}원까지만 담습니다`} />
            </div>
            <div className="row" style={{ marginTop: 16 }}>
              <AmountField id="loss" label="일일 손실 한도" value={loss} onChange={setLoss} prefix="−" unit="원" hint="이 전략의 손실이 닿으면 그날 신규 매수를 멈춥니다" />
              <AmountField id="orders" label="하루 최대 주문 수" value={orders} onChange={setOrders} unit="회" hint="과도한 매매와 수수료를 막아 줍니다 (손절 주문은 예외)" />
            </div>
          </Section>

          <Section id="s4" n={4} title="주문은 어떻게 낼까요?">
            <div role="group" aria-label="실행 방식" className="row" style={{ gap: 12, marginTop: 16 }}>
              {EXECS.map((x) => (
                <button key={x.id} type="button" className="choice" style={{ flex: '1 1 220px' }} aria-pressed={x.id === exec} onClick={() => setExec(x.id)}>
                  <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ fontWeight: 600 }}>{x.label}</span>
                    {x.badge && <span className="xs accent" style={{ fontWeight: 600 }}>{x.badge}</span>}
                  </span>
                  <span className="muted" style={{ display: 'block', marginTop: 4, fontSize: 13 }}>{x.desc}</span>
                </button>
              ))}
            </div>
            <div className="hint">계좌 전체 자동화 모드(대시보드)가 더 보수적이면 그 모드를 따릅니다.</div>
          </Section>
        </div>

        <aside aria-label="백테스트 결과와 시작" className="col-side" style={{ flexBasis: 360 }}>
          <Backtest result={bt} loading={btLoading} name={tpl.name} stop={stop} weight={weight} />
          <section aria-labelledby="go-h" className="card strong" style={{ padding: 22 }}>
            <h2 id="go-h">시작하기</h2>
            <dl className="dl" style={{ gap: 6 }}>
              <div><dt>전략</dt><dd>{tpl.name}</dd></div>
              <div><dt>투자 대상</dt><dd>{uni === 'custom' ? `직접 고른 ${custom.length}종목` : uniDef.label}</dd></div>
              <div><dt>맡길 금액</dt><dd className="mono">{won(amountNum)}원</dd></div>
              <div><dt>실행 방식</dt><dd>{execDef.label}</dd></div>
            </dl>
            {create.error && <div className="hint error">{create.error}</div>}
            <button type="button" className="btn btn-primary btn-lg btn-block" style={{ marginTop: 16 }} disabled={create.busy || !!amountError || (uni === 'custom' && !custom.length)} onClick={start}>
              {isLive ? '전략 시작하기' : `${meta.broker.label}로 시작하기`}
            </button>
            {!isLive && (
              <>
                <button type="button" className="btn btn-ghost btn-block" style={{ marginTop: 8, minHeight: 48 }} aria-disabled="true" disabled>
                  실전 투자로 바로 시작
                </button>
                <div className="hint" style={{ textAlign: 'center' }}>
                  {meta.live_unlocked
                    ? '모의투자 운영 기간을 채웠어요 · 실전 계좌 연결(KIWOOM_MODE=real) 후 설정에서 전환할 수 있어요'
                    : `실전 전환은 모의투자 ${meta.paper_days_required}일 운영 후 열려요 · 지금 ${meta.paper_days}일째`}
                </div>
              </>
            )}
            {isLive && <div className="hint" style={{ textAlign: 'center' }}>실전 계좌입니다 · 설정에서 실전 주문을 허용하기 전까지는 알림만 보냅니다</div>}
          </section>
        </aside>
      </div>
    </main>
  )
}
