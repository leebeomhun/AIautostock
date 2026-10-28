export type Side = 'buy' | 'sell'
export type Mode = 'notify' | 'approve' | 'auto'
export type Tone = 'buy' | 'sell' | 'warn' | 'info' | 'good' | 'muted'

export interface Meta {
  app_name: string
  broker: { kind: 'kiwoom' | 'local'; label: string; is_live: boolean }
  market_source: string
  ai: { enabled: boolean; model: string }
}

export interface StrategyRow {
  id: number
  name: string
  template: string
  status: 'running' | 'paused' | 'stopped'
  status_label: string
  universe_label: string
  alloc_pct: number
  invested_pct: number
  today_pct: number
  positions: number
  note: string | null
  exec_mode: Mode
}

export interface Holding {
  symbol: string
  name: string
  qty: number
  avg_price: number
  price: number
  pnl: number
  pnl_pct: number
  strategy: string
  ai_note: string
  ai_tone: Tone
  decision_id: string | null
}

export interface FeedItem {
  id: number
  time: string
  tag: string
  tone: Tone
  title: string
  detail: string
  decision_id: string | null
  pending: boolean
  decided_text: string | null
  expires_at: string | null
}

export interface PendingDecision {
  id: string
  symbol: string
  name: string
  side: Side
  qty: number
  price: number
  amount: number
  pnl: number | null
  confidence: number
  summary: string
  strategy: string
  expires_at: string | null
}

export interface Dashboard extends Meta {
  clock: { now: string; date_label: string; last_sync: string | null }
  market: { open: boolean; label: string }
  onboarded: boolean
  error: string | null
  ready: boolean
  status?: {
    running: boolean
    mode: Mode
    effective_mode: Mode
    mode_label: string
    strategies_running: number
    strategies_paused: number
    cancelled_on_stop: number
    live_locked: boolean
  }
  loss?: { used: number; limit: number; pct: number; action: 'buy' | 'all' }
  summary?: {
    account_label: string
    equity: number
    change: number
    change_pct: number
    realized_today: number
    fills_today: number
    cash: number
    cash_pct: number
    win_rate: number | null
    wins: number
    losses: number
    avg_hold_days: number | null
  }
  strategies?: StrategyRow[]
  holdings?: Holding[]
  feed?: FeedItem[]
  pending?: PendingDecision[]
  notifications?: number
}

export interface Performance {
  period: string
  points: { t: string; ai: number; bench: number }[]
  labels: string[]
  ai_return: number | null
  bench_return: number | null
  excess: number | null
}

export interface SignalItem {
  key: string
  name: string
  detail: string
  side: 'buy' | 'sell' | 'hold' | 'neutral'
  strength: number
  label: string
}

export interface DecisionDetail extends Meta {
  error: string | null
  id: string
  symbol: string
  name: string
  side: Side
  reason: string
  status: string
  status_label: string
  status_note: string | null
  qty: number
  max_qty: number
  position_qty: number
  price: number
  current_price: number
  avg_price: number | null
  confidence: number
  summary: string
  caution: string
  signals: SignalItem[]
  history_stat: { count: number; hit_rate: number; horizon: number; side: Side } | null
  chart: {
    dates: string[]
    closes: number[]
    avg?: number
    stop?: number
    take?: number
    high20?: number
    news?: { title: string; source: string; url: string; sentiment: number }[]
  } | null
  risk_rules: string[]
  risk_notes: string[]
  model: string
  strategy: { id: number; name: string; label: string; template: string; stop_pct: number; take_pct: number; model_id: string } | null
  data_at: string
  created_at: string
  decided_at: string | null
  decided_by: string | null
  feedback: string | null
  mode: Mode
  expires_at: string | null
  seconds_left: number | null
  timeout_seconds: number
  order: { order_no: string; status: string; qty: number; filled_qty: number; fill_price: number | null; realized_pnl: number | null; message: string | null } | null
  impact_base: { equity: number; cash: number; holdings_count: number; realized_today: number; strategy_invested: number; already_held: boolean }
  fee_rate: number
  sell_tax_rate: number
  ledger: LedgerEntry[]
  research: ResearchItem | null
}

export interface LedgerEntry {
  seq: number
  at: string
  kind: string
  payload: Record<string, unknown>
  prev_hash: string
  hash: string
}

export interface ResearchItem {
  symbol: string
  name: string
  rating: 'Buy' | 'Overweight' | 'Hold' | 'Underweight' | 'Sell'
  confidence: number
  summary: string
  as_of: string
  model: string
  engine: string
  sources: { title: string; url: string }[]
  report_text?: string | null
}

export interface DecisionRow {
  id: string
  created_at: string
  symbol: string
  name: string
  side: Side
  qty: number
  price: number
  confidence: number
  status: string
  status_label: string
  strategy: string
  summary: string
  model: string
  fill_price: number | null
  realized_pnl: number | null
}

export interface TemplateDef {
  id: 'momentum' | 'meanrev' | 'news' | 'value'
  name: string
  desc: string
  hold: string
  freq: string
  risk: '높음' | '중간' | '낮음'
  version: string
  available: boolean
}

export interface UniverseDef {
  id: 'k200' | 'kq150' | 'sp500' | 'custom'
  label: string
  available: boolean
  note: string | null
  symbols: { symbol: string; name: string }[]
}

export interface StrategiesMeta extends Meta {
  templates: TemplateDef[]
  universes: UniverseDef[]
  pickable_symbols: { symbol: string; name: string }[]
  cash: number | null
  equity: number | null
  allocated: number
  paper_days: number
  paper_days_required: number
  live_unlocked: boolean
  defaults: { daily_loss_limit: number }
}

export interface BacktestResult {
  ok: boolean
  message: string | null
  start: string | null
  end: string | null
  curve: { date: string; strategy: number; benchmark: number }[]
  cagr: number
  mdd: number
  win_rate: number
  trades: number
  orders: number
  total_return: number
  benchmark_return: number
  sharpe: number
  volatility: number
  worst_month: { label: string; ret: number } | null
  note: string | null
  symbols: number
  data_source: string
}

export interface SettingsView extends Meta {
  error: string | null
  account_label: string | null
  cash: number | null
  equity: number | null
  onboarded: boolean
  risk_profile: 'conservative' | 'neutral' | 'aggressive'
  invest_amount: number
  daily_loss_limit: number
  limit_action: 'buy' | 'all'
  agreed: boolean
  mode: Mode
  running: boolean
  paper_started_at: string | null
  paper_days: number
  paper_days_required: number
  live_confirmed: boolean
  live_unlocked: boolean
  has_strategy: boolean
  ml: { as_of: string; model: string; count: number; metrics: { valid_rank_ic: number; valid_ic: number; train_days: number; symbols: number } } | null
  research: { as_of: string; model: string; count: number } | null
}

export interface Report {
  period: string
  metrics: { total_return: number | null; volatility: number | null; max_drawdown: number | null; sharpe: number | null; bench_return: number | null }
  trades: { count: number; wins: number; win_rate: number | null; realized: number; best: number | null; worst: number | null; avg: number | null }
  by_strategy: { name: string; realized: number; trades: number; wins: number }[]
  performance: Performance
}

export const AUTH_EVENT = 'autostock:auth-required'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (res.status === 401 && !path.startsWith('/auth/')) {
    // 세션이 끝나면 로그인 화면으로 돌아갑니다.
    window.dispatchEvent(new Event(AUTH_EVENT))
  }
  if (!res.ok) {
    let message = `요청 실패 (HTTP ${res.status})`
    try {
      const data = await res.json()
      if (typeof data.detail === 'string') message = data.detail
      else if (Array.isArray(data.detail)) message = data.detail.map((d: { msg: string }) => d.msg).join(', ')
    } catch {
      /* 본문이 JSON 이 아니면 기본 메시지 */
    }
    throw new ApiError(res.status, message)
  }
  return res.json() as Promise<T>
}

export const api = {
  authStatus: () => request<{ required: boolean; authenticated: boolean }>('GET', '/auth/status'),
  login: (password: string) => request<{ ok: boolean }>('POST', '/auth/login', { password }),
  logout: () => request<{ ok: boolean }>('POST', '/auth/logout'),
  dashboard: () => request<Dashboard>('GET', '/dashboard'),
  performance: (period: string) => request<Performance>('GET', `/performance?period=${period}`),
  decision: (id: string) => request<DecisionDetail>('GET', `/decisions/${encodeURIComponent(id)}`),
  decisions: () => request<DecisionRow[]>('GET', '/decisions?limit=200'),
  approve: (id: string, qty?: number) => request<DecisionDetail>('POST', `/decisions/${encodeURIComponent(id)}/approve`, { qty }),
  reject: (id: string, feedback?: string) => request<DecisionDetail>('POST', `/decisions/${encodeURIComponent(id)}/reject`, { feedback }),
  reopen: (id: string) => request<DecisionDetail>('POST', `/decisions/${encodeURIComponent(id)}/reopen`),
  cancelOrder: (id: string) => request<{ message: string }>('POST', `/decisions/${encodeURIComponent(id)}/cancel-order`),
  setMode: (mode: Mode) => request<{ ok: boolean }>('POST', '/control/mode', { mode }),
  stop: () => request<{ cancelled: number }>('POST', '/control/stop'),
  start: () => request<{ ok: boolean }>('POST', '/control/start'),
  strategiesMeta: () => request<StrategiesMeta>('GET', '/strategies/meta'),
  backtest: (body: unknown) => request<BacktestResult>('POST', '/backtest', body),
  createStrategy: (body: unknown) => request<{ id: number }>('POST', '/strategies', body),
  patchStrategy: (id: number, status: string) => request<unknown>('PATCH', `/strategies/${id}`, { status }),
  settings: () => request<SettingsView>('GET', '/settings'),
  saveSettings: (body: Partial<SettingsView>) => request<SettingsView>('PUT', '/settings', body),
  completeOnboarding: () => request<SettingsView>('POST', '/onboarding/complete'),
  confirmLive: () => request<SettingsView>('POST', '/settings/live-confirm'),
  report: (period: string) => request<Report>('GET', `/report?period=${period}`),
  verifyLedger: () => request<{ ok: boolean; count: number; broken_at: number | null; head: string }>('GET', '/ledger/verify'),
  research: (symbol: string) => request<ResearchItem>('GET', `/insights/research/${encodeURIComponent(symbol)}`),
}
