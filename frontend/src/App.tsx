import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { Shell } from './components/Shell'
import { Dashboard } from './pages/Dashboard'
import { Decision } from './pages/Decision'
import { History } from './pages/History'
import { Onboarding } from './pages/Onboarding'
import { Report } from './pages/Report'
import { Settings } from './pages/Settings'
import { StrategyNew } from './pages/StrategyNew'

export default function App() {
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
