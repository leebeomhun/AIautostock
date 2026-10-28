import { createContext, useCallback, useContext, useState, type ReactNode } from 'react'
import { Link, NavLink, Navigate, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { api, type Dashboard, type Meta } from '../api'
import { useAction, usePolling } from '../hooks'
import { Bell, Gear, Home, List, LogoMark, Power, Trend, UserIcon } from './Icons'

interface ShellValue {
  dash: Dashboard | null
  refresh: () => Promise<void>
  toast: (text: string, action?: { label: string; onClick: () => void }) => void
}

const ShellContext = createContext<ShellValue | null>(null)

export function useShell(): ShellValue {
  const v = useContext(ShellContext)
  if (!v) throw new Error('useShell 은 Shell 안에서만 씁니다')
  return v
}

const NAV = [
  { to: '/', label: '대시보드', end: true },
  { to: '/strategies/new', label: '전략' },
  { to: '/history', label: '매매 내역' },
  { to: '/report', label: '리포트' },
  { to: '/settings', label: '설정' },
]

export function BrokerBadge({ dash }: { dash: Meta | null }) {
  if (!dash) return null
  const live = dash.broker.is_live
  return <span className={`chip ${live ? 'chip-up' : 'chip-warn'}`}>{dash.broker.label}</span>
}

function KillButton({ dash, onDone }: { dash: Dashboard; onDone: () => void }) {
  const { busy, run } = useAction()
  const running = dash.status?.running ?? false
  return (
    <button
      type="button"
      className={`btn ${running ? 'btn-danger' : 'btn-primary'} hide-sm`}
      disabled={busy}
      onClick={() => run<unknown>(() => (running ? api.stop() : api.start())).then(onDone)}
    >
      <Power size={16} />
      {running ? '긴급 정지' : '다시 시작'}
    </button>
  )
}

function Header({ dash, refresh }: { dash: Dashboard | null; refresh: () => void }) {
  const { pathname } = useLocation()
  const navigate = useNavigate()
  const onDashboard = pathname === '/'
  const pendingCount = dash?.notifications ?? 0
  const firstPending = dash?.pending?.[0]
  return (
    <header className="header">
      <div className="header-inner">
        <Link to="/" className="brand">
          <span className="logo"><LogoMark /></span>
          <span>{dash?.app_name ?? 'AutoStock'}</span>
        </Link>
        <nav aria-label="주요 메뉴" className="nav">
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.end} className={({ isActive }) => (isActive ? 'active' : '')}>
              {n.label}
            </NavLink>
          ))}
        </nav>
        <div className="header-right">
          {dash && onDashboard && (
            <span className="chip hide-sm">
              <span className={`dot ${dash.market.open ? '' : 'idle'}`} />
              {dash.market.label}
            </span>
          )}
          {dash?.status && !onDashboard && (
            <span className="chip hide-sm">
              <span className={`dot ${dash.status.running ? '' : 'off'}`} />
              {dash.status.running ? `AI 실행 중 · ${dash.status.mode_label}` : 'AI 정지됨'}
            </span>
          )}
          <BrokerBadge dash={dash} />
          {dash?.status && !onDashboard && <KillButton dash={dash} onDone={refresh} />}
          <button
            type="button"
            className="icon-btn"
            aria-label={pendingCount ? `승인 대기 ${pendingCount}건` : '알림 없음'}
            onClick={() => navigate(firstPending ? `/decisions/${firstPending.id}` : '/history')}
          >
            <Bell />
            {pendingCount > 0 && <span className="badge-count">{pendingCount}</span>}
          </button>
          <Link to="/settings" className="icon-btn round hide-sm" aria-label="계정·설정">
            <UserIcon />
          </Link>
        </div>
      </div>
    </header>
  )
}

function BottomNav() {
  const items = [
    { to: '/', label: '홈', icon: <Home />, end: true },
    { to: '/strategies/new', label: '전략', icon: <Trend /> },
    { to: '/history', label: '내역', icon: <List /> },
    { to: '/settings', label: '설정', icon: <Gear /> },
  ]
  return (
    <nav aria-label="하단 메뉴" className="bottom-nav">
      {items.map((i) => (
        <NavLink key={i.to} to={i.to} end={i.end} className={({ isActive }) => (isActive ? 'active' : '')}>
          {i.icon}
          {i.label}
        </NavLink>
      ))}
    </nav>
  )
}

export function Shell() {
  const { data: dash, refresh } = usePolling(api.dashboard, 5000)
  const [toastState, setToast] = useState<{ text: string; action?: { label: string; onClick: () => void } } | null>(null)
  const { pathname } = useLocation()

  const toast = useCallback((text: string, action?: { label: string; onClick: () => void }) => {
    setToast({ text, action })
    window.setTimeout(() => setToast((t) => (t?.text === text ? null : t)), 6000)
  }, [])

  if (dash && !dash.onboarded && pathname !== '/onboarding') return <Navigate to="/onboarding" replace />

  return (
    <ShellContext.Provider value={{ dash, refresh, toast }}>
      <div className="app">
        <Header dash={dash} refresh={refresh} />
        <Outlet />
        <BottomNav />
        {toastState && (
          <div role="status" className="toast">
            <span>{toastState.text}</span>
            {toastState.action && (
              <button
                type="button"
                className="btn-link"
                onClick={() => {
                  toastState.action?.onClick()
                  setToast(null)
                }}
              >
                {toastState.action.label}
              </button>
            )}
          </div>
        )}
      </div>
    </ShellContext.Provider>
  )
}

export function PageError({ message }: { message: string | null | undefined }): ReactNode {
  if (!message) return null
  return (
    <div role="alert" className="banner error">
      <span>{message}</span>
    </div>
  )
}
