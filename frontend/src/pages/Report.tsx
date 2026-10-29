import { useState } from 'react'
import { api, type AiReviewSide } from '../api'
import { LineChart } from '../components/Charts'
import { PageError } from '../components/Shell'
import { signedPct, signedWon, trendClass } from '../format'
import { usePolling } from '../hooks'

const PERIODS = [
  { id: '1w', label: '1주' },
  { id: '1m', label: '1개월' },
  { id: '3m', label: '3개월' },
  { id: '1y', label: '1년' },
]

function ReviewRow({ label, side, r }: { label: string; side: 'buy' | 'sell'; r: AiReviewSide }) {
  const cell = (x: { count: number; avg_return: number | null }) => (
    <span className={`mono ${trendClass(x.avg_return)}`}>{x.count ? `${signedPct(x.avg_return, 1)} · ${x.count}건` : '—'}</span>
  )
  return (
    <tr>
      <td>{label}</td>
      <td className="num">{cell(r.followed)}</td>
      <td className="num">{cell(r.held)}</td>
      <td className={`num mono ${trendClass(r.edge)}`}>{r.edge == null ? '—' : `${signedPct(r.edge, 1).replace('%', '%p')}`}</td>
      <td className="xs dim">{side === 'buy' ? '낸 매수가 보류한 매수보다 더 올랐으면 +' : '보류한 매도가 낸 매도보다 더 올랐으면 +'}</td>
    </tr>
  )
}

function Metric({ label, value, cls = '', foot }: { label: string; value: string; cls?: string; foot?: string }) {
  return (
    <div className="card stat">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${cls}`} style={{ fontSize: 22 }}>{value}</div>
      {foot && <div className="stat-foot">{foot}</div>}
    </div>
  )
}

export function Report() {
  const [period, setPeriod] = useState('1m')
  const { data, error } = usePolling(() => api.report(period), 30000, [period])
  const m = data?.metrics
  const t = data?.trades
  const perf = data?.performance
  return (
    <main className="page">
      <div className="page-title">
        <h1>운용 리포트</h1>
        <div role="group" aria-label="기간 선택" className="segmented compact">
          {PERIODS.map((p) => (
            <button key={p.id} type="button" aria-pressed={p.id === period} onClick={() => setPeriod(p.id)}>{p.label}</button>
          ))}
        </div>
      </div>
      <PageError message={error} />
      {data && m && t && (
        <>
          <div className="row">
            <Metric label="기간 수익률" value={signedPct(m.total_return)} cls={trendClass(m.total_return)} foot={m.bench_return != null ? `KOSPI ${signedPct(m.bench_return)}` : undefined} />
            <Metric label="최대 낙폭 (MDD)" value={signedPct(m.max_drawdown)} cls="down" />
            <Metric label="연환산 변동성" value={m.volatility == null ? '—' : `${m.volatility}%`} foot="일별 평가자산 기준 (5일 이상)" />
            <Metric label="샤프 지수" value={m.sharpe == null ? '—' : m.sharpe.toFixed(2)} foot="무위험수익률 0 가정" />
          </div>
          <section className="card" aria-labelledby="rep-chart-h">
            <h2 id="rep-chart-h">평가자산 추이 · AI 포트폴리오 vs KOSPI</h2>
            {perf && perf.points.length >= 2 ? (
              <>
                <div style={{ marginTop: 16 }}>
                  <LineChart
                    label={`AI 포트폴리오 ${signedPct(perf.ai_return)}, KOSPI ${signedPct(perf.bench_return)}`}
                    series={[
                      { values: perf.points.map((p) => p.bench), color: '#6B7383' },
                      { values: perf.points.map((p) => p.ai), color: '#C8F04A', width: 2.5, fill: true },
                    ]}
                  />
                </div>
                <div className="axis">{perf.labels.map((l, i) => <span key={i}>{l}</span>)}</div>
              </>
            ) : (
              <div className="empty">평가자산 기록이 쌓이면 표시됩니다.</div>
            )}
          </section>
          <div className="row top">
            <section className="card" style={{ flex: '1 1 340px' }} aria-labelledby="rep-trade-h">
              <h2 id="rep-trade-h">청산 거래</h2>
              <dl className="dl">
                <div><dt>거래 수</dt><dd className="mono">{t.count}건 · {t.wins}승</dd></div>
                <div><dt>승률</dt><dd className="mono">{t.win_rate == null ? '—' : `${t.win_rate}%`}</dd></div>
                <div><dt>실현손익 합계</dt><dd className={`mono ${trendClass(t.realized)}`}>{signedWon(t.realized)}원</dd></div>
                <div><dt>평균 · 최고 · 최저</dt><dd className="mono">{signedWon(t.avg)} · {signedWon(t.best)} · {signedWon(t.worst)}</dd></div>
              </dl>
            </section>
            <section className="card" style={{ flex: '2 1 480px' }} aria-labelledby="rep-strat-h">
              <h2 id="rep-strat-h">전략별 실현손익</h2>
              {data.by_strategy.length === 0 ? (
                <div className="empty">이 기간에 청산한 거래가 없습니다.</div>
              ) : (
                <div className="table-wrap">
                  <table className="data" style={{ minWidth: 420 }}>
                    <thead><tr><th scope="col">전략</th><th scope="col" className="num">거래</th><th scope="col" className="num">승</th><th scope="col" className="num">실현손익</th></tr></thead>
                    <tbody>
                      {data.by_strategy.map((r) => (
                        <tr key={r.name}><td>{r.name}</td><td className="num">{r.trades}</td><td className="num">{r.wins}</td><td className={`num ${trendClass(r.realized)}`}>{signedWon(r.realized)}</td></tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          </div>
          <section className="card" aria-labelledby="rep-ai-h">
            <h2 id="rep-ai-h">AI 판단 점검 · 신호 후 {data.ai_review.horizon}거래일 등락</h2>
            <p className="xs dim" style={{ margin: '0 0 10px' }}>
              퀀트 규칙이 낸 후보 중 AI가 그대로 낸 것과 보류한 것의 이후 등락을 비교합니다. 규칙 기반 설명과 손절은 뺐고, {data.ai_review.horizon}거래일이 지난 판단만 셉니다.
            </p>
            <div className="table-wrap">
              <table className="data" style={{ minWidth: 560 }}>
                <thead><tr><th scope="col">구분</th><th scope="col" className="num">AI가 낸 판단</th><th scope="col" className="num">AI가 보류한 판단</th><th scope="col" className="num">차이</th><th scope="col">읽는 법</th></tr></thead>
                <tbody>
                  <ReviewRow label="매수" side="buy" r={data.ai_review.buy} />
                  <ReviewRow label="매도" side="sell" r={data.ai_review.sell} />
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
      <p className="disclaimer">지표 정의는 gs-quant 시계열 함수(volatility, max_drawdown, sharpe)와 같습니다. 과거 성과는 미래 수익을 보장하지 않습니다.</p>
    </main>
  )
}
