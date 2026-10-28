import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type Dashboard as DashboardData, type FeedItem, type Holding, type StrategyRow } from '../api'
import { ApprovalSheet } from '../components/ApprovalSheet'
import { LineChart } from '../components/Charts'
import { Plus } from '../components/Icons'
import { PageError, useShell } from '../components/Shell'
import { StatusPanel } from '../components/StatusPanel'
import { arrowPct, signedPct, signedWon, trendClass, won } from '../format'
import { useAction, useMediaQuery, usePolling } from '../hooks'

const PERIODS = [
  { id: '1w', label: '1주' },
  { id: '1m', label: '1개월' },
  { id: '3m', label: '3개월' },
  { id: '1y', label: '1년' },
]

function Stat({ label, value, unit = '원', valueClass = '', foot }: { label: string; value: string; unit?: string; valueClass?: string; foot: React.ReactNode }) {
  return (
    <div className="card stat">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${valueClass}`}>
        {value}
        <span className="unit">{unit}</span>
      </div>
      <div className="stat-foot">{foot}</div>
    </div>
  )
}

function Summary({ dash }: { dash: DashboardData }) {
  const s = dash.summary!
  const loss = dash.loss!
  return (
    <div className="row">
      <div className="card stat">
        <div className="stat-label">총 평가자산</div>
        <div className="stat-value">
          {won(s.equity)}
          <span className="unit">원</span>
        </div>
        <div className={`stat-foot mono ${trendClass(s.change)}`}>
          {s.change > 0 ? '▲' : s.change < 0 ? '▼' : ''} {won(Math.abs(s.change))} ({signedPct(s.change_pct)}) <span className="dim" style={{ fontFamily: 'var(--font)' }}>오늘</span>
        </div>
        <div className="show-sm divider-top" style={{ marginTop: 12, paddingTop: 12 }}>
          <div className="spread small">
            <span className="muted">일일 손실 한도</span>
            <span className="mono">{loss.pct}% 사용</span>
          </div>
          <div className="meter track" style={{ marginTop: 6 }}>
            <div style={{ width: `${Math.min(100, loss.pct)}%`, background: 'var(--accent)' }} />
          </div>
        </div>
      </div>
      <Stat label="오늘 실현손익" value={signedWon(s.realized_today)} valueClass={trendClass(s.realized_today)} foot={`체결 ${s.fills_today}건 · 수수료·세금 차감 후`} />
      <Stat label="주문 가능 현금" value={won(s.cash)} foot={`자산 대비 ${s.cash_pct}% · ${s.account_label}`} />
      <Stat
        label="AI 승률 · 최근 30일"
        value={s.win_rate == null ? '—' : s.win_rate.toFixed(1)}
        unit="%"
        foot={s.wins + s.losses ? `${s.wins}승 ${s.losses}패${s.avg_hold_days != null ? ` · 평균 보유 ${s.avg_hold_days}일` : ''}` : '아직 청산한 거래가 없습니다'}
      />
    </div>
  )
}

function PerformanceCard() {
  const [period, setPeriod] = useState('1m')
  const { data } = usePolling(() => api.performance(period), 30000, [period])
  const label = PERIODS.find((p) => p.id === period)!.label
  const hasData = data && data.points.length >= 2
  return (
    <section aria-labelledby="perf-h" className="card" style={{ flex: '999 1 620px' }}>
      <div className="spread wrap" style={{ alignItems: 'flex-start', gap: 12 }}>
        <div>
          <h2 id="perf-h">수익률 비교 · 최근 {label}</h2>
          {hasData && (
            <div className="legend">
              <span className="legend-item">
                <span className="swatch" />
                <span className="muted">AI 포트폴리오</span>
                <span className={`mono ${trendClass(data.ai_return)}`} style={{ fontWeight: 600 }}>{arrowPct(data.ai_return, 1)}</span>
              </span>
              <span className="legend-item">
                <span className="swatch bench" />
                <span className="muted">KOSPI</span>
                <span className={`mono ${trendClass(data.bench_return)}`} style={{ fontWeight: 600 }}>{arrowPct(data.bench_return, 1)}</span>
              </span>
              {data.excess != null && (
                <span className={`pill ${data.excess >= 0 ? 'good' : 'info'}`} style={{ fontWeight: 600 }}>
                  초과수익 {signedPct(data.excess, 1).replace('%', '%p')}
                </span>
              )}
            </div>
          )}
        </div>
        <div role="group" aria-label="기간 선택" className="segmented compact">
          {PERIODS.map((p) => (
            <button key={p.id} type="button" aria-pressed={p.id === period} onClick={() => setPeriod(p.id)}>
              {p.label}
            </button>
          ))}
        </div>
      </div>
      {hasData ? (
        <>
          <div style={{ marginTop: 18 }}>
            <LineChart
              label={`최근 ${label} AI 포트폴리오 ${signedPct(data.ai_return, 1)}, 같은 기간 KOSPI ${signedPct(data.bench_return, 1)}`}
              series={[
                { values: data.points.map((p) => p.bench), color: '#6B7383', width: 2 },
                { values: data.points.map((p) => p.ai), color: '#C8F04A', width: 2.5, fill: true },
              ]}
            />
          </div>
          <div className="axis">
            {data.labels.map((l, i) => (
              <span key={i}>{l}</span>
            ))}
          </div>
        </>
      ) : (
        <div className="empty">운용 기록이 쌓이면 AI 포트폴리오와 KOSPI 수익률을 비교해 보여드려요.</div>
      )}
    </section>
  )
}

function StrategiesCard({ strategies }: { strategies: StrategyRow[] }) {
  return (
    <section aria-labelledby="strat-h" className="card" style={{ flex: '1 1 340px', display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div className="spread">
        <h2 id="strat-h">운영 중인 전략</h2>
        <span className="small dim">자금 배분 기준</span>
      </div>
      {strategies.length === 0 && <div className="empty">아직 전략이 없습니다.</div>}
      {strategies.map((s) => {
        const color = s.status === 'running' ? 'var(--accent)' : 'var(--warn)'
        return (
          <div key={s.id} className="inner">
            <div className="spread">
              <span style={{ fontWeight: 600 }}>{s.name}</span>
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12, color }}>
                <span className="dot" style={{ background: color }} />
                {s.status_label}
              </span>
            </div>
            {s.note ? (
              <div className="muted" style={{ marginTop: 4, fontSize: 13 }}>{s.note}</div>
            ) : (
              <div className="spread muted" style={{ marginTop: 4, fontSize: 13 }}>
                <span>
                  {s.universe_label} · 배분 {s.alloc_pct}% · 보유 {s.positions}종목
                </span>
                <span className={`mono ${trendClass(s.today_pct)}`}>{arrowPct(s.today_pct)}</span>
              </div>
            )}
            <div className="meter thin" style={{ marginTop: 10 }}>
              <div style={{ width: `${Math.min(100, s.alloc_pct)}%`, background: s.status === 'running' ? 'var(--accent)' : 'var(--text-dim)' }} />
            </div>
          </div>
        )
      })}
      <Link
        to="/strategies/new"
        style={{ marginTop: 'auto', minHeight: 44, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8, border: '1px dashed var(--border-strong)', borderRadius: 10, fontSize: 13, fontWeight: 600 }}
      >
        <Plus />
        새 전략 만들기
      </Link>
    </section>
  )
}

function HoldingsCard({ holdings }: { holdings: Holding[] }) {
  return (
    <section aria-labelledby="hold-h" className="card" style={{ flex: '999 1 620px' }}>
      <div className="spread wrap">
        <h2 id="hold-h">
          보유 종목 <span className="dim" style={{ fontWeight: 500 }}>{holdings.length}</span>
        </h2>
        <Link to="/history" style={{ fontSize: 13, padding: '10px 0' }}>
          매매 내역 →
        </Link>
      </div>
      {holdings.length === 0 ? (
        <div className="empty">보유 중인 종목이 없습니다.</div>
      ) : (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th scope="col">종목</th>
                <th scope="col" className="num">수량</th>
                <th scope="col" className="num">평균단가</th>
                <th scope="col" className="num">현재가</th>
                <th scope="col" className="num">평가손익</th>
                <th scope="col" className="num">수익률</th>
                <th scope="col" className="gap">진입 전략</th>
                <th scope="col">AI 의견</th>
              </tr>
            </thead>
            <tbody>
              {holdings.map((h) => (
                <tr key={h.symbol}>
                  <td>
                    <div style={{ fontWeight: 600 }}>{h.name}</div>
                    <div className="sym-code">{h.symbol}</div>
                  </td>
                  <td className="num">{h.qty.toLocaleString('ko-KR')}</td>
                  <td className="num">{won(h.avg_price)}</td>
                  <td className="num">{won(h.price)}</td>
                  <td className={`num ${trendClass(h.pnl)}`}>{signedWon(h.pnl)}</td>
                  <td className={`num ${trendClass(h.pnl_pct)}`}>{arrowPct(h.pnl_pct)}</td>
                  <td className="gap muted">{h.strategy}</td>
                  <td>
                    {h.decision_id ? (
                      <Link to={`/decisions/${h.decision_id}`} className={`pill ${h.ai_tone}`}>
                        {h.ai_note}
                      </Link>
                    ) : (
                      <span className={`pill ${h.ai_tone}`}>{h.ai_note}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

function FeedEntry({ f, onChange }: { f: FeedItem; onChange: () => void }) {
  const { busy, error, run } = useAction()
  return (
    <div className="feed-item">
      <div className="feed-time">{f.time}</div>
      <div className="feed-body">
        <div className="feed-head">
          <span className={`pill sm ${f.tone}`}>{f.tag}</span>
          <span style={{ fontWeight: 600 }}>{f.title}</span>
        </div>
        {f.detail && <div className="feed-detail">{f.detail}</div>}
        {f.pending && f.decision_id && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 10 }}>
            <button type="button" className="btn btn-primary" style={{ fontSize: 13 }} disabled={busy} onClick={() => run(() => api.approve(f.decision_id!)).then(onChange)}>
              승인
            </button>
            <button type="button" className="btn btn-ghost" style={{ fontSize: 13 }} disabled={busy} onClick={() => run(() => api.reject(f.decision_id!)).then(onChange)}>
              거절
            </button>
            <Link to={`/decisions/${f.decision_id}`} className="btn-link">
              근거 보기 →
            </Link>
          </div>
        )}
        {error && <div className="hint error">{error}</div>}
        {f.decided_text && <div className="feed-decided">{f.decided_text}</div>}
        {!f.pending && f.decision_id && (
          <Link to={`/decisions/${f.decision_id}`} style={{ display: 'inline-block', marginTop: 4, padding: '6px 0', fontSize: 13 }}>
            판단 근거 보기 →
          </Link>
        )}
      </div>
    </div>
  )
}

function FeedCard({ feed, onChange }: { feed: FeedItem[]; onChange: () => void }) {
  return (
    <section aria-labelledby="feed-h" className="card" style={{ flex: '1 1 340px' }}>
      <div className="spread">
        <h2 id="feed-h">AI 활동 기록</h2>
        <span className="small dim">모든 판단에 근거가 남습니다</span>
      </div>
      <div style={{ marginTop: 10 }}>
        {feed.length === 0 && <div className="empty">아직 기록이 없습니다. 엔진이 다음 주기에 종목을 살펴봅니다.</div>}
        {feed.map((f) => (
          <FeedEntry key={f.id} f={f} onChange={onChange} />
        ))}
      </div>
    </section>
  )
}

export function Dashboard() {
  const { dash, refresh } = useShell()
  const isMobile = useMediaQuery('(max-width: 720px)')
  const [dismissed, setDismissed] = useState<string | null>(null)
  const pending = dash?.pending?.[0]

  if (!dash) return <main className="page"><div className="empty">불러오는 중…</div></main>
  return (
    <main className="page">
      <div className="page-title">
        <h1>오늘의 운용 현황</h1>
        <span className="small dim mono">
          {dash.clock.date_label} · 마지막 동기화 {dash.clock.last_sync ?? '—'}
        </span>
      </div>
      <PageError message={dash.error} />
      {dash.ready && dash.status && (
        <>
          <StatusPanel dash={dash} onChange={refresh} />
          <Summary dash={dash} />
          <div className="row">
            <PerformanceCard />
            <StrategiesCard strategies={dash.strategies ?? []} />
          </div>
          <div className="row top">
            <HoldingsCard holdings={dash.holdings ?? []} />
            <FeedCard feed={dash.feed ?? []} onChange={refresh} />
          </div>
        </>
      )}
      <p className="disclaimer">AI의 매매 신호는 수익을 보장하지 않으며, 투자 판단과 결과의 책임은 투자자 본인에게 있습니다.</p>
      {isMobile && pending && dismissed !== pending.id && (
        <ApprovalSheet decision={pending} onClose={() => setDismissed(pending.id)} onDone={refresh} />
      )}
    </main>
  )
}
