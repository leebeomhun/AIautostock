import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { PageError } from '../components/Shell'
import { dateTime, sideLabel, signedWon, trendClass, won } from '../format'
import { usePolling } from '../hooks'

const FILTERS = [
  { id: 'all', label: '전체' },
  { id: 'pending', label: '승인 대기' },
  { id: 'executed', label: '주문됨' },
  { id: 'closed', label: '거절·취소' },
]

const TONE: Record<string, string> = { pending: 'warn', executed: 'sell', failed: 'buy', notified: 'info', held: 'info' }

export function History() {
  const { data, error } = usePolling(api.decisions, 10000)
  const [filter, setFilter] = useState('all')
  const rows = (data ?? []).filter((d) =>
    filter === 'all' ? true : filter === 'closed' ? ['rejected', 'expired', 'cancelled', 'held'].includes(d.status) : d.status === filter,
  )
  return (
    <main className="page">
      <div className="page-title">
        <h1>매매 내역</h1>
        <span className="small dim">AI가 낸 모든 판단과 그 결과입니다</span>
      </div>
      <PageError message={error} />
      <section className="card" aria-labelledby="hist-h">
        <div className="spread wrap">
          <h2 id="hist-h">판단 {rows.length}건</h2>
          <div role="group" aria-label="상태 필터" className="segmented compact">
            {FILTERS.map((f) => (
              <button key={f.id} type="button" aria-pressed={f.id === filter} onClick={() => setFilter(f.id)}>
                {f.label}
              </button>
            ))}
          </div>
        </div>
        {rows.length === 0 ? (
          <div className="empty">{data ? '해당하는 판단이 없습니다.' : '불러오는 중…'}</div>
        ) : (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th scope="col">시각</th>
                  <th scope="col">종목</th>
                  <th scope="col">구분</th>
                  <th scope="col" className="num">수량</th>
                  <th scope="col" className="num">가격</th>
                  <th scope="col" className="num">신뢰도</th>
                  <th scope="col" className="num">실현손익</th>
                  <th scope="col" className="gap">전략</th>
                  <th scope="col">상태</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((d) => (
                  <tr key={d.id}>
                    <td className="mono dim" style={{ whiteSpace: 'nowrap' }}>{dateTime(d.created_at)}</td>
                    <td>
                      <Link to={`/decisions/${d.id}`} style={{ color: 'var(--text)', fontWeight: 600 }}>{d.name}</Link>
                      <div className="sym-code">{d.symbol}</div>
                    </td>
                    <td className={d.side === 'buy' ? 'up' : 'down'}>{sideLabel(d.side)} {d.side === 'buy' ? '▲' : '▼'}</td>
                    <td className="num">{d.qty}</td>
                    <td className="num">{won(d.fill_price ?? d.price)}</td>
                    <td className="num">{d.confidence}%</td>
                    <td className={`num ${trendClass(d.realized_pnl)}`}>{d.realized_pnl == null ? '—' : signedWon(d.realized_pnl)}</td>
                    <td className="gap muted">{d.strategy}</td>
                    <td><Link to={`/decisions/${d.id}`} className={`pill ${TONE[d.status] ?? 'info'}`}>{d.status_label}</Link></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </main>
  )
}
