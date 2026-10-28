import { useState } from 'react'
import { api } from '../api'
import { LogoMark } from '../components/Icons'
import { useAction } from '../hooks'

export function Login({ onDone }: { onDone: () => void }) {
  const [password, setPassword] = useState('')
  const { busy, error, run } = useAction()
  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    run(() => api.login(password)).then((r) => r && onDone())
  }
  return (
    <main style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 16 }}>
      <form onSubmit={submit} className="card" style={{ width: '100%', maxWidth: 380, padding: '28px 26px', borderRadius: 16 }} aria-labelledby="login-h">
        <div className="brand" style={{ fontSize: 18 }}>
          <span className="logo"><LogoMark /></span>
          AutoStock
        </div>
        <h1 id="login-h" style={{ marginTop: 22, fontSize: 20, fontWeight: 700 }}>로그인</h1>
        <p className="muted" style={{ marginTop: 6, fontSize: 13 }}>주문을 승인하고 멈출 수 있는 화면이라 비밀번호로 보호합니다.</p>
        <label htmlFor="pw" className="field-label" style={{ display: 'block', marginTop: 20 }}>비밀번호</label>
        <div className="input-box">
          <input id="pw" type="password" autoComplete="current-password" autoFocus value={password} onChange={(e) => setPassword(e.target.value)} style={{ fontFamily: 'var(--font)' }} />
        </div>
        {error && <div role="alert" className="hint error">{error}</div>}
        <button type="submit" className="btn btn-primary btn-lg btn-block" style={{ marginTop: 18 }} disabled={busy || !password}>
          로그인
        </button>
      </form>
    </main>
  )
}
