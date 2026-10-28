import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, type DecisionDetail, type ResearchItem, type SignalItem } from '../api'
import { PriceChart } from '../components/Charts'
import { Markdown } from '../components/Markdown'
import { PageError, useShell } from '../components/Shell'
import { clock, dateTime, mmss, sideLabel, signedWon, trendClass, won } from '../format'
import { useAction, useNow, usePolling } from '../hooks'

const SIDE_COLOR: Record<SignalItem['side'], string> = {
  sell: 'var(--down)',
  buy: 'var(--up)',
  hold: 'var(--text-soft)',
  neutral: 'var(--text-dim)',
}

const ORDER_STATUS: Record<string, string> = {
  submitted: '접수',
  partial: '일부 체결',
  filled: '체결',
  cancelled: '취소',
  rejected: '거부',
}

const STATUS_TONE: Record<string, string> = {
  pending: 'warn',
  executed: 'sell',
  notified: 'info',
  rejected: 'info',
  expired: 'info',
  cancelled: 'info',
  held: 'info',
  failed: 'buy',
}

function qtyChoices(d: DecisionDetail): number[] {
  const max = d.max_qty
  const base = new Set<number>([d.qty, max])
  if (max >= 3) {
    base.add(Math.max(1, Math.round(max / 3)))
    base.add(Math.max(1, Math.round((max * 2) / 3)))
  }
  return [...base].filter((n) => n >= 1 && n <= max).sort((a, b) => a - b).slice(-3)
}

function Countdown({ d }: { d: DecisionDetail }) {
  const now = useNow(d.status === 'pending')
  if (d.status !== 'pending' || !d.expires_at) return null
  const left = (new Date(d.expires_at).getTime() - now) / 1000
  const pct = Math.max(0, Math.min(100, (left / d.timeout_seconds) * 100))
  return (
    <div style={{ minWidth: 260 }}>
      <div className="spread" style={{ fontSize: 13 }}>
        <span className="muted">응답이 없으면 자동 취소</span>
        <span className="mono warn" style={{ fontWeight: 600 }}>{mmss(left)}</span>
      </div>
      <div className="meter track" style={{ marginTop: 8 }}>
        <div style={{ width: `${pct}%`, background: 'var(--warn)' }} />
      </div>
    </div>
  )
}

function Signals({ d }: { d: DecisionDetail }) {
  const mainSide = d.side
  return (
    <section aria-labelledby="sig-h" className="card">
      <div className="spread wrap">
        <h2 id="sig-h">근거 시그널</h2>
        <div style={{ display: 'flex', gap: 14, fontSize: 12 }} className="muted">
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            <span className="legend-box" style={{ background: SIDE_COLOR[mainSide] }} />
            {sideLabel(mainSide)} 쪽 근거
          </span>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            <span className="legend-box" style={{ background: SIDE_COLOR.hold }} />
            {mainSide === 'sell' ? '보유' : '관망'} 쪽 근거
          </span>
        </div>
      </div>
      <div className="stack" style={{ marginTop: 12 }}>
        {d.signals.map((s) => (
          <div key={s.key} className="signal-row">
            <div className="signal-text">
              <div style={{ fontWeight: 600 }}>{s.name}</div>
              <div className="muted" style={{ fontSize: 13 }}>{s.detail}</div>
            </div>
            <div className="signal-bar">
              <div className="meter track">
                <div style={{ width: `${Math.max(4, s.strength * 100)}%`, background: SIDE_COLOR[s.side], borderRadius: 3 }} />
              </div>
              <span>{s.label}</span>
            </div>
          </div>
        ))}
      </div>
      {d.chart?.news && d.chart.news.length > 0 && (
        <div className="divider-top" style={{ marginTop: 4, paddingTop: 12 }}>
          <div className="small muted">뉴스 출처 (OpenAI 웹 검색)</div>
          <ul style={{ margin: '6px 0 0', paddingLeft: 18, fontSize: 13 }}>
            {d.chart.news.map((n) => (
              <li key={n.url} style={{ marginTop: 4 }}>
                <a href={n.url} target="_blank" rel="noreferrer noopener">{n.title}</a>
                <span className="dim"> · {n.source} · 감성 {n.sentiment > 0 ? '+' : ''}{n.sentiment.toFixed(2)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}

function Chart({ d }: { d: DecisionDetail }) {
  const c = d.chart
  if (!c || c.closes.length < 2) return null
  const lines: { value: number; color: string; text: string }[] = []
  if (c.avg) lines.push({ value: c.avg, color: '#9AA3B2', text: `평균단가 ${won(c.avg)}` })
  if (c.stop && d.side === 'sell') lines.push({ value: c.stop, color: '#7DB8FF', text: `손절 기준 ${won(c.stop)} (−${d.strategy?.stop_pct ?? ''}%)` })
  if (c.high20 && d.side === 'buy') lines.push({ value: c.high20, color: '#FF5C5C', text: `직전 20일 고점 ${won(c.high20)}` })
  const first = c.closes[0]
  const last = c.closes[c.closes.length - 1]
  return (
    <section aria-labelledby="chart-h" className="card">
      <h2 id="chart-h">가격 흐름 · 최근 {c.closes.length}거래일</h2>
      <PriceChart
        dates={c.dates}
        closes={c.closes}
        lines={lines}
        markerColor={d.side === 'sell' ? '#7DB8FF' : '#FF5C5C'}
        label={`${d.name} 주가가 ${c.closes.length}거래일 동안 ${won(first)}원에서 ${won(last)}원으로 ${last >= first ? '올랐습니다' : '내렸습니다'}`}
      />
    </section>
  )
}

const RATING_KO: Record<ResearchItem['rating'], string> = { Buy: '매수', Overweight: '비중 확대', Hold: '보유', Underweight: '비중 축소', Sell: '매도' }
const RATING_TONE: Record<ResearchItem['rating'], string> = { Buy: 'buy', Overweight: 'buy', Hold: 'info', Underweight: 'sell', Sell: 'sell' }

function Research({ d }: { d: DecisionDetail }) {
  const [open, setOpen] = useState(false)
  const [report, setReport] = useState<string | null>(null)
  const { busy, error, run } = useAction()
  const r = d.research
  if (!r) return null
  const toggle = () => {
    if (!open && report == null) run(() => api.research(d.symbol)).then((x) => x && setReport(x.report_text ?? '리포트 파일을 찾지 못했습니다.'))
    setOpen(!open)
  }
  return (
    <section aria-labelledby="res-h" className="card">
      <div className="spread wrap">
        <h2 id="res-h">심층 리서치</h2>
        <span className="small dim">{r.as_of} · {r.engine === 'tradingagents' ? 'TradingAgents' : '멀티 에이전트(TradingAgents 구성)'}</span>
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8, marginTop: 12 }}>
        <span className={`pill ${RATING_TONE[r.rating]}`} style={{ fontWeight: 600 }}>{RATING_KO[r.rating]} · {r.rating}</span>
        <span className="small muted">신뢰도 {Math.round(r.confidence * 100)}%</span>
      </div>
      <p style={{ marginTop: 10, lineHeight: 1.6 }}>{r.summary}</p>
      <button type="button" className="btn-link" style={{ paddingLeft: 0 }} aria-expanded={open} onClick={toggle} disabled={busy}>
        {open ? '리포트 접기' : '전체 리포트 보기 →'}
      </button>
      {error && <div className="hint error">{error}</div>}
      {open && report != null && (
        <div className="inner" style={{ marginTop: 8, fontSize: 13, maxHeight: 520, overflowY: 'auto' }}>
          <Markdown text={report} />
        </div>
      )}
    </section>
  )
}

function OrderPanel({ d, qty, setQty, onChange }: { d: DecisionDetail; qty: number; setQty: (n: number) => void; onChange: (next?: DecisionDetail) => void }) {
  const { toast } = useShell()
  const { busy, error, run } = useAction()
  const pending = d.status === 'pending'
  const amount = qty * d.price
  const pnl = d.avg_price != null && d.side === 'sell' ? qty * (d.price - d.avg_price) : null
  const side = sideLabel(d.side)
  const choices = qtyChoices(d)
  return (
    <section aria-labelledby="order-h" className="card strong">
      <h2 id="order-h">주문 내용</h2>
      <dl className="dl" style={{ fontSize: 14 }}>
        <div>
          <dt>구분</dt>
          <dd style={{ fontWeight: 600 }} className={d.side === 'buy' ? 'up' : 'down'}>
            {side} {d.side === 'buy' ? '▲' : '▼'}
          </dd>
        </div>
        <div>
          <dt>주문 방식</dt>
          <dd>시장가</dd>
        </div>
        <div>
          <dt>예상 체결가</dt>
          <dd className="mono">{won(d.price)}원</dd>
        </div>
      </dl>
      <div style={{ marginTop: 14 }}>
        <div className="field-label">수량{d.side === 'sell' ? ` · 보유 ${d.position_qty}주` : ` · 최대 ${d.max_qty}주`}</div>
        <div role="group" aria-label={`${side} 수량`} style={{ display: 'flex', gap: 6, marginTop: 8 }}>
          {choices.map((n) => (
            <button
              key={n}
              type="button"
              className="chip-btn"
              style={{ flex: 1, borderRadius: 8 }}
              aria-pressed={n === qty}
              disabled={!pending}
              onClick={() => setQty(n)}
            >
              {n === d.position_qty && d.side === 'sell' ? `${n}주 (전량)` : `${n}주`}
            </button>
          ))}
        </div>
      </div>
      <div className="divider-top" style={{ marginTop: 16, paddingTop: 14, display: 'flex', flexDirection: 'column', gap: 8, borderColor: 'var(--border)' }}>
        <div className="spread">
          <span className="muted">예상 {side} 금액</span>
          <span className="mono" style={{ fontSize: 18, fontWeight: 600 }}>{won(amount)}원</span>
        </div>
        {pnl != null && (
          <div className="spread">
            <span className="muted">예상 실현손익</span>
            <span className={`mono ${trendClass(pnl)}`} style={{ fontWeight: 600 }}>{signedWon(pnl)}원</span>
          </div>
        )}
        <div className="hint" style={{ marginTop: 0 }}>수수료·세금 별도 · 실제 체결가는 달라질 수 있습니다</div>
      </div>
      {error && <div className="hint error">{error}</div>}
      {pending && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 18 }}>
          <button
            type="button"
            className="btn btn-primary btn-lg"
            disabled={busy}
            onClick={() =>
              run(() => api.approve(d.id, qty)).then((next) => {
                if (next) {
                  onChange(next)
                  toast(`${side} 주문을 보냈습니다`)
                }
              })
            }
          >
            승인하고 {side}
          </button>
          <button type="button" className="btn btn-ghost" style={{ minHeight: 48 }} disabled={busy} onClick={() => run(() => api.reject(d.id)).then((next) => next && onChange(next))}>
            거절하고 {d.side === 'sell' ? '계속 보유' : '매수 안 함'}
          </button>
        </div>
      )}
      {!pending && (
        <div style={{ marginTop: 18, padding: '12px 14px', borderRadius: 10, background: 'var(--surface-3)', fontSize: 13 }} className="muted">
          {d.status === 'executed'
            ? '주문이 전송되어 수량을 바꿀 수 없습니다. 체결 전이라면 위 배너에서 취소할 수 있어요.'
            : d.status === 'rejected'
              ? d.side === 'sell'
                ? '보유를 유지합니다. 손절 기준을 다시 넘으면 새 제안을 드릴게요.'
                : '매수하지 않습니다. 조건이 다시 맞으면 새 제안을 드릴게요.'
              : d.status === 'notified'
                ? '알림만 모드라 주문을 내지 않았습니다. 직접 주문하시려면 증권사 앱을 이용해 주세요.'
                : d.status_note ?? '처리가 끝난 판단입니다.'}
        </div>
      )}
    </section>
  )
}

function Impact({ d, qty }: { d: DecisionDetail; qty: number }) {
  const b = d.impact_base
  if (!b.equity) return null
  const amount = qty * d.price
  const sign = d.side === 'sell' ? 1 : -1
  const cashBefore = Math.round((b.cash / b.equity) * 100)
  const cashAfter = Math.round(((b.cash + sign * amount) / b.equity) * 100)
  const holdAfter = d.side === 'sell' ? (qty >= d.position_qty ? b.holdings_count - 1 : b.holdings_count) : b.already_held ? b.holdings_count : b.holdings_count + 1
  const pnl = d.side === 'sell' && d.avg_price != null ? qty * (d.price - d.avg_price) : 0
  const allocBefore = Math.round((b.strategy_invested / b.equity) * 100)
  const allocAfter = Math.round(((b.strategy_invested - sign * amount) / b.equity) * 100)
  return (
    <section aria-labelledby="impact-h" className="card">
      <h2 id="impact-h">승인하면 계좌는 이렇게 바뀝니다</h2>
      <div className="stack" style={{ marginTop: 12, gap: 10, fontSize: 13 }}>
        <div className="spread"><span className="muted">현금 비중</span><span className="mono">{cashBefore}% → <strong>{cashAfter}%</strong></span></div>
        <div className="spread"><span className="muted">보유 종목 수</span><span className="mono">{b.holdings_count} → <strong>{holdAfter}</strong></span></div>
        {d.side === 'sell' && (
          <div className="spread"><span className="muted">오늘 실현손익</span><span className="mono">{signedWon(b.realized_today)} → <strong>{signedWon(b.realized_today + pnl)}</strong></span></div>
        )}
        <div className="spread"><span className="muted">{d.strategy?.name ?? '전략'} 투자 비중</span><span className="mono">{allocBefore}% → <strong>{Math.max(0, allocAfter)}%</strong></span></div>
      </div>
    </section>
  )
}

function DecisionLog({ d }: { d: DecisionDetail }) {
  const log = JSON.stringify(d, null, 2)
  const href = useMemo(() => URL.createObjectURL(new Blob([log], { type: 'application/json' })), [log])
  useEffect(() => () => URL.revokeObjectURL(href), [href])
  return (
    <section aria-labelledby="log-h" className="card">
      <h2 id="log-h">판단 기록</h2>
      <dl className="dl" style={{ gap: 8 }}>
        <div><dt>판단 ID</dt><dd className="mono">{d.id}</dd></div>
        <div><dt>전략 · 버전</dt><dd className="mono">{d.strategy ? `${d.strategy.model_id} ${d.strategy.label.split(' ').pop()}` : '—'}</dd></div>
        <div><dt>AI 모델</dt><dd className="mono">{d.model}</dd></div>
        <div><dt>데이터 기준 시각</dt><dd className="mono">{clock(d.data_at)}</dd></div>
        <div><dt>적용된 리스크 규칙</dt><dd>{d.risk_rules.join(' · ')}</dd></div>
        {d.decided_at && <div><dt>처리 시각</dt><dd className="mono">{dateTime(d.decided_at)} · {d.decided_by === 'user' ? '사용자' : d.decided_by === 'auto' ? '자동' : '시스템'}</dd></div>}
        {d.order && <div><dt>주문번호</dt><dd className="mono">{d.order.order_no} · {ORDER_STATUS[d.order.status] ?? d.order.status}</dd></div>}
      </dl>
      {d.risk_notes.length > 0 && (
        <ul style={{ margin: '10px 0 0', paddingLeft: 18, fontSize: 12 }} className="dim">
          {d.risk_notes.map((n) => <li key={n}>{n}</li>)}
        </ul>
      )}
      <LedgerView d={d} />
      <a href={href} download={`${d.id}.json`} style={{ display: 'inline-flex', alignItems: 'center', minHeight: 44, marginTop: 6, fontSize: 13 }}>
        판단 로그 내려받기 →
      </a>
    </section>
  )
}

const LEDGER_KIND: Record<string, string> = {
  'decision.created': '판단 생성',
  'decision.held': 'AI 보류',
  'decision.approved': '사용자 승인',
  'decision.rejected': '사용자 거절',
  'decision.reopened': '다시 검토',
  'decision.expired': '시간 초과',
  'decision.cancelled': '취소',
  'order.submitted': '주문 전송',
  'order.failed': '주문 실패',
  'order.filled': '체결',
  'order.cancelled': '주문 취소',
  'order.rejected': '주문 거부',
}

function LedgerView({ d }: { d: DecisionDetail }) {
  const { data } = usePolling(api.verifyLedger, 60000)
  if (!d.ledger.length) return null
  return (
    <div className="divider-top" style={{ marginTop: 12, paddingTop: 12 }}>
      <div className="spread small">
        <span className="muted">해시 체인 원장</span>
        {data && (
          <span className={data.ok ? 'accent' : 'up'} style={{ fontWeight: 600 }}>
            {data.ok ? `검증됨 · 전체 ${data.count}건` : `변조 의심 · ${data.broken_at}번 기록`}
          </span>
        )}
      </div>
      <ol style={{ listStyle: 'none', margin: '8px 0 0', padding: 0, display: 'flex', flexDirection: 'column', gap: 6, fontSize: 12 }}>
        {d.ledger.map((e) => (
          <li key={e.seq} className="spread" style={{ gap: 8 }}>
            <span>
              <span className="mono dim">#{e.seq}</span> {LEDGER_KIND[e.kind] ?? e.kind}
            </span>
            <span className="mono dim" title={e.hash}>{clock(e.at)} · {e.hash.slice(0, 10)}…</span>
          </li>
        ))}
      </ol>
    </div>
  )
}

function ResultBanner({ d, onChange }: { d: DecisionDetail; onChange: (next?: DecisionDetail) => void }) {
  const { busy, error, run } = useAction()
  const now = useNow(d.status === 'rejected')
  const side = sideLabel(d.side)
  if (d.status === 'executed') {
    const o = d.order
    const filled = o && o.status === 'filled'
    return (
      <div role="status" className={`banner ${d.side}`}>
        <span style={{ fontWeight: 600 }}>
          {filled
            ? `${side} 체결 · ${o!.filled_qty}주 ${won(o!.fill_price)}원${o!.realized_pnl != null ? ` · 실현손익 ${signedWon(o!.realized_pnl)}원` : ''}`
            : `${side} 주문을 보냈습니다 · ${d.qty}주 시장가. 체결되면 알림으로 알려드릴게요.`}
        </span>
        {o && (o.status === 'submitted' || o.status === 'partial') && (
          <button type="button" className="btn btn-ghost" style={{ fontSize: 13 }} disabled={busy} onClick={() => run(() => api.cancelOrder(d.id)).then(() => onChange())}>
            체결 전 주문 취소
          </button>
        )}
        {error && <span className="hint error">{error}</span>}
      </div>
    )
  }
  if (d.status === 'rejected') {
    return (
      <div role="status" className="banner">
        <span style={{ fontWeight: 600 }}>제안을 거절했습니다 · {d.side === 'sell' ? '계속 보유하며' : '매수하지 않으며'}, 이 결정은 AI 피드백으로 기록됩니다.</span>
        {d.decided_by === 'user' && d.expires_at && new Date(d.expires_at).getTime() > now && (
          <button type="button" className="btn btn-ghost" style={{ fontSize: 13 }} disabled={busy} onClick={() => run(() => api.reopen(d.id)).then((n) => n && onChange(n))}>
            다시 검토하기
          </button>
        )}
        {error && <span className="hint error">{error}</span>}
      </div>
    )
  }
  if (d.status === 'failed') return <div role="alert" className="banner error">주문이 거부됐습니다 · {d.status_note}</div>
  if (['expired', 'cancelled', 'held'].includes(d.status)) return <div role="status" className="banner">{d.status_label} · {d.status_note ?? d.summary}</div>
  return null
}

export function Decision() {
  const { id = '' } = useParams()
  const { refresh: refreshShell } = useShell()
  const { data: d, error, refresh, setData } = usePolling(() => api.decision(id), 5000, [id])
  // 화면에서 고른 수량은 그 판단에만 적용합니다.
  const [picked, setPicked] = useState<{ id: string; qty: number } | null>(null)
  if (error && !d) return <main className="page"><PageError message={error} /></main>
  if (!d) return <main className="page"><div className="empty">불러오는 중…</div></main>
  const chosen = picked?.id === d.id ? picked.qty : d.qty
  const setQty = (n: number) => setPicked({ id: d.id, qty: n })
  const onChange = (next?: DecisionDetail) => {
    if (next) setData(next)
    refresh()
    refreshShell()
  }
  const side = sideLabel(d.side)
  return (
    <main className="page" style={{ paddingTop: 20 }}>
      <nav aria-label="현재 위치" className="small dim" style={{ display: 'flex', flexWrap: 'wrap', gap: 6, fontSize: 13 }}>
        <Link to="/history" style={{ color: 'var(--text-2)' }}>매매 내역</Link>
        <span aria-hidden="true">/</span>
        <span style={{ color: 'var(--text)' }}>{d.name} {side} 제안</span>
      </nav>
      <PageError message={d.error} />
      <ResultBanner d={d} onChange={onChange} />
      <div className="spread wrap" style={{ alignItems: 'flex-end', gap: '12px 24px' }}>
        <div>
          <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8 }}>
            <span className={`pill ${STATUS_TONE[d.status] ?? 'info'}`} style={{ fontWeight: 600 }}>{d.status_label}</span>
            <span className="dim" style={{ fontSize: 13 }}>{d.strategy?.label} · {dateTime(d.created_at)} 생성</span>
          </div>
          <h1 style={{ marginTop: 8, fontSize: 26, fontWeight: 700, letterSpacing: '-0.02em' }}>
            {d.name} <span className="mono dim" style={{ fontSize: 16, fontWeight: 500 }}>{d.symbol}</span> {side} 제안
          </h1>
        </div>
        <Countdown d={d} />
      </div>
      <div className="row top">
        <div className="col-main">
          <section aria-labelledby="sum-h" className="card" style={{ padding: '22px 24px' }}>
            <h2 id="sum-h" style={{ fontSize: 13, color: 'var(--text-2)' }}>AI 판단 요약</h2>
            <p style={{ marginTop: 10, fontSize: 19, lineHeight: 1.6, fontWeight: 500 }}>{d.summary}</p>
            {d.caution && <p className="warn" style={{ marginTop: 8, fontSize: 13 }}>주의 · {d.caution}</p>}
            <div className="row" style={{ gap: '20px 40px', marginTop: 20 }}>
              <div style={{ flex: '1 1 280px' }}>
                <div className="spread base">
                  <span className="muted" style={{ fontSize: 13 }}>신뢰도</span>
                  <span className="mono" style={{ fontSize: 20, fontWeight: 600 }}>{d.confidence}%</span>
                </div>
                <div className="meter" style={{ height: 10, marginTop: 8, border: 'none', background: 'var(--surface-3)' }}>
                  <div style={{ width: `${d.confidence}%`, borderRadius: 5 }} />
                </div>
                <div className="spread xs dim" style={{ marginTop: 6 }}><span>낮음</span><span>보통</span><span>높음</span></div>
              </div>
              <div className="inner muted" style={{ flex: '1 1 280px', padding: '12px 14px', fontSize: 13 }}>
                {d.history_stat ? (
                  <>
                    이 전략이 비슷한 조건에서 낸 과거 {sideLabel(d.history_stat.side)} 신호{' '}
                    <span className="mono" style={{ color: 'var(--text)' }}>{d.history_stat.count.toLocaleString('ko-KR')}</span>건 중{' '}
                    <span className="mono" style={{ color: 'var(--text)' }}>{d.history_stat.hit_rate}%</span>는 이후 {d.history_stat.horizon}거래일 안에{' '}
                    {d.history_stat.side === 'sell' ? '추가 하락' : '상승'}했습니다.
                  </>
                ) : (
                  '비교할 만한 과거 신호가 충분하지 않습니다.'
                )}
              </div>
            </div>
          </section>
          <Signals d={d} />
          <Research d={d} />
          <Chart d={d} />
        </div>
        <div className="col-side" style={{ flexBasis: 360 }}>
          <OrderPanel d={d} qty={chosen} setQty={setQty} onChange={onChange} />
          {d.status === 'pending' && <Impact d={d} qty={chosen} />}
          <DecisionLog d={d} />
        </div>
      </div>
    </main>
  )
}
