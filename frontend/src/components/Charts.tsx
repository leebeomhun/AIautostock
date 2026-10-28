interface Series {
  values: number[]
  color: string
  width?: number
  fill?: boolean
}

const W = 800
const H = 220

function scale(all: number[], top = 20, bottom = 200) {
  const lo = Math.min(...all)
  const hi = Math.max(...all)
  const span = hi - lo || Math.abs(hi) || 1
  const min = lo - span * 0.08
  const max = hi + span * 0.08
  return (v: number) => bottom - ((v - min) / (max - min)) * (bottom - top)
}

/** 수익률 비교처럼 여러 선을 같은 축에 그리는 반응형 선 그래프. */
export function LineChart({ series, height = 240, label, grid = true }: { series: Series[]; height?: number; label: string; grid?: boolean }) {
  const all = series.flatMap((s) => s.values)
  if (all.length < 2) return null
  const y = scale(all)
  const toPoints = (values: number[]) =>
    values.map((v, i) => `${values.length === 1 ? 0 : (i / (values.length - 1)) * W},${y(v).toFixed(1)}`).join(' ')
  return (
    <svg role="img" aria-label={label} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={{ display: 'block', width: '100%', height }}>
      {grid &&
        [40, 90, 140, 190].map((g) => (
          <line key={g} x1="0" y1={g} x2={W} y2={g} stroke="#232833" strokeWidth="1" vectorEffect="non-scaling-stroke" />
        ))}
      {series.map((s, i) => {
        const pts = toPoints(s.values)
        return (
          <g key={i}>
            {s.fill && <polygon points={`${pts} ${W},${H} 0,${H}`} fill={s.color} fillOpacity="0.07" />}
            <polyline points={pts} fill="none" stroke={s.color} strokeWidth={s.width ?? 2} strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
          </g>
        )
      })}
    </svg>
  )
}

/** 판단 근거 화면의 최근 10거래일 가격 흐름 (평균단가·손절선 표시). */
export function PriceChart({
  dates,
  closes,
  lines,
  markerColor,
  label,
}: {
  dates: string[]
  closes: number[]
  lines: { value: number; color: string; text: string }[]
  markerColor: string
  label: string
}) {
  if (closes.length < 2) return null
  const width = 760
  const left = 40
  const right = 730
  const y = scale([...closes, ...lines.map((l) => l.value)], 30, 190)
  const x = (i: number) => left + (i / (closes.length - 1)) * (right - left)
  const pts = closes.map((c, i) => `${x(i).toFixed(1)},${y(c).toFixed(1)}`).join(' ')
  const last = closes.length - 1
  const tickIdx = Array.from(new Set([0, Math.round(last / 3), Math.round((2 * last) / 3), last]))
  return (
    <svg role="img" aria-label={label} viewBox={`0 0 ${width} 245`} style={{ display: 'block', width: '100%', height: 'auto', marginTop: 12 }}>
      {lines.map((l) => (
        <g key={l.text}>
          <line x1={left} y1={y(l.value)} x2={right} y2={y(l.value)} stroke={l.color} strokeWidth="1.2" strokeDasharray="5 5" />
          <text x={left} y={y(l.value) - 8} fill={l.color} fontSize="12" fontFamily="IBM Plex Sans KR, sans-serif">
            {l.text}
          </text>
        </g>
      ))}
      <polyline points={pts} fill="none" stroke="#ECEEF2" strokeWidth="2.4" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={x(last)} cy={y(closes[last])} r="6" fill={markerColor} stroke="#161A21" strokeWidth="3" />
      <text x={x(last)} y={Math.min(225, y(closes[last]) + 28)} fill="#ECEEF2" fontSize="12" textAnchor="end" fontFamily="IBM Plex Mono, monospace">
        {Math.round(closes[last]).toLocaleString('ko-KR')}
      </text>
      {tickIdx.map((i) => (
        <text key={i} x={x(i)} y="240" fill="#9AA3B2" fontSize="11" textAnchor={i === 0 ? 'start' : i === last ? 'end' : 'middle'} fontFamily="IBM Plex Mono, monospace">
          {dates[i]}
        </text>
      ))}
    </svg>
  )
}
