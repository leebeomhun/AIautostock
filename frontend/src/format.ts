const MINUS = '−'

/** 52,480,300 */
export function won(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return '—'
  const v = Math.round(n)
  return (v < 0 ? MINUS : '') + Math.abs(v).toLocaleString('ko-KR')
}

/** +412,500 / −288,000 */
export function signedWon(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return '—'
  const v = Math.round(n)
  return (v > 0 ? '+' : v < 0 ? MINUS : '') + Math.abs(v).toLocaleString('ko-KR')
}

/** ▲ 5.06% / ▼ 1.75% — 색만으로 구분하지 않도록 기호를 붙입니다. */
export function arrowPct(n: number | null | undefined, digits = 2): string {
  if (n == null || Number.isNaN(n)) return '—'
  const arrow = n > 0 ? '▲ ' : n < 0 ? '▼ ' : ''
  return `${arrow}${Math.abs(n).toFixed(digits)}%`
}

/** +1.84% / −0.31% */
export function signedPct(n: number | null | undefined, digits = 2): string {
  if (n == null || Number.isNaN(n)) return '—'
  return (n > 0 ? '+' : n < 0 ? MINUS : '') + Math.abs(n).toFixed(digits) + '%'
}

/** 양수=상승(빨강) 음수=하락(파랑) */
export function trendClass(n: number | null | undefined): string {
  if (!n) return ''
  return n > 0 ? 'up' : 'down'
}

export function parseAmount(text: string): number {
  return Number(String(text).replace(/[^0-9]/g, '')) || 0
}

export function formatAmountInput(text: string): string {
  const n = parseAmount(text)
  return n ? n.toLocaleString('ko-KR') : ''
}

export function clock(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false, timeZone: 'Asia/Seoul' })
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleString('ko-KR', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Seoul' })
}

export function mmss(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

export const sideLabel = (side: 'buy' | 'sell') => (side === 'buy' ? '매수' : '매도')
