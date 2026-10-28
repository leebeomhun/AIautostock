import { useEffect, useState } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { api, AUTH_EVENT } from './api'
import { Shell } from './components/Shell'
import { Dashboard } from './pages/Dashboard'
import { Decision } from './pages/Decision'
import { History } from './pages/History'
import { Login } from './pages/Login'
import { Onboarding } from './pages/Onboarding'
import { Report } from './pages/Report'
import { Settings } from './pages/Settings'
import { StrategyNew } from './pages/StrategyNew'

type AuthState = 'checking' | 'in' | 'out'

export default function App() {
  const [auth, setAuth] = useState<AuthState>('checking')
  useEffect(() => {
    api.authStatus().then(
      (s) => setAuth(s.authenticated ? 'in' : 'out'),
      () => setAuth('in'), // 상태 확인이 안 되면 화면을 열고, 각 요청의 401 로 다시 판단합니다.
    )
    const onExpired = () => setAuth('out')
    window.addEventListener(AUTH_EVENT, onExpired)
    return () => window.removeEventListener(AUTH_EVENT, onExpired)
  }, [])

  if (auth === 'checking') return null
  if (auth === 'out') return <Login onDone={() => setAuth('in')} />
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/onboarding" element={<Onboarding />} />
        <Route element={<Shell />}>
          <Route index element={<Dashboard />} />
          <Route path="decisions/:id" element={<Decision />} />
          <Route path="history" element={<History />} />
          <Route path="strategies/new" element={<StrategyNew />} />
          <Route path="report" element={<Report />} />
          <Route path="settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
