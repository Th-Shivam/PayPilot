import { useEffect, useRef, useState } from 'react'
import type { FormEvent, ReactElement } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { IntroScreen } from './components/IntroScreen'
import { SignInPage } from './pages/SignInPage'
import { SignUpPage } from './pages/SignUpPage'
import './App.css'
import './auth.css'
import {
  ApiClientError,
  apiFetch,
  streamResolve,
} from './lib/api'
import type { ResolveResponse, TraceEvent } from './lib/api'
import { authConfigured, supabase, type AuthSession } from './lib/supabase'

const INTRO_DURATION_MS = 4000

function readableName(name: string): string {
  return name.replaceAll('_', ' ')
}

function statusLabel(status: TraceEvent['status']): string {
  return status.replaceAll('_', ' ')
}

function ResolutionApp({ session }: { session: AuthSession | null }): ReactElement {
  const [txnId, setTxnId] = useState('txn-clean-001')
  const [events, setEvents] = useState<TraceEvent[]>([])
  const [resolution, setResolution] = useState<ResolveResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const controller = useRef<AbortController | null>(null)

  const appendEvent = (event: TraceEvent): void => {
    setEvents((current) => {
      if (current.some((item) => item.event_id === event.event_id)) return current
      return [...current, event].sort((left, right) => left.step_number - right.step_number)
    })
  }

  const resolve = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault()
    const value = txnId.trim()
    if (!value || running) return
    controller.current?.abort()
    const nextController = new AbortController()
    controller.current = nextController
    setEvents([])
    setResolution(null)
    setError(null)
    setRunning(true)
    try {
      setResolution(await streamResolve(value, appendEvent, nextController.signal))
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === 'AbortError') return
      setError(cause instanceof ApiClientError ? cause.message : 'The resolution stream failed.')
    } finally {
      setRunning(false)
    }
  }

  const replay = async (): Promise<void> => {
    const value = txnId.trim()
    if (!value || running) return
    setError(null)
    try {
      const trace = await apiFetch<{ request_id: string; run_id: string; created_at: string; steps: TraceEvent[] }>('trace/' + encodeURIComponent(value))
      setEvents([...trace.steps].sort((left, right) => left.step_number - right.step_number))
      const completion = trace.steps.find((item) => item.event_type === 'completion' && item.status === 'completed')
      const result = completion?.detail.resolution
      if (result && typeof result === 'object') {
        const payload = result as Record<string, unknown>
        setResolution({
          txn_id: String(payload.txn_id ?? value),
          transaction_id: String(payload.transaction_id ?? value),
          status: String(payload.status ?? 'unknown'),
          explanation: String(payload.explanation ?? completion.summary),
          action: String(payload.action ?? 'no_action_needed'),
          trace,
        })
      }
    } catch (cause) {
      setError(cause instanceof ApiClientError ? cause.message : 'The saved trace could not be loaded.')
    }
  }

  return (
    <main className="shell">
      <header className="masthead">
        <div>
          <p className="eyebrow">PAYPILOT / OPERATIONS</p>
          <h1>Transaction resolution</h1>
          <p className="lede">Watch the real gateway, bank, ledger, and decision trail as it completes.</p>
        </div>
        <div className="header-actions">
          <div className={'connection ' + (running ? 'is-running' : '')} aria-live="polite"><span className="connection-dot" aria-hidden="true" />{running ? 'Resolving' : 'Ready'}</div>
          {session && supabase ? <button className="secondary" type="button" onClick={() => { if (supabase) void supabase.auth.signOut() }}>Sign out</button> : null}
        </div>
      </header>

      <section className="query-band" aria-label="Transaction query">
        <form className="query-form" onSubmit={resolve}>
          <label htmlFor="txn-id">Transaction ID</label>
          <div className="query-controls">
            <input id="txn-id" value={txnId} onChange={(input) => setTxnId(input.target.value)} placeholder="txn-clean-001" autoComplete="off" spellCheck={false} />
            <button type="submit" disabled={running || !txnId.trim()}>{running ? 'Working...' : 'Resolve transaction'}</button>
            <button className="secondary" type="button" onClick={replay} disabled={running || !txnId.trim()}>Replay saved trace</button>
          </div>
        </form>
      </section>

      {error ? <div className="alert" role="alert"><strong>Resolution unavailable.</strong> {error}</div> : null}
      <section className="workspace">
        <div className="trace-column">
          <div className="section-heading"><div><p className="eyebrow">LIVE TRACE</p><h2>Agent workflow</h2></div><span className="event-count" aria-label={events.length + ' trace events'}>{events.length} events</span></div>
          <div className="trace-panel" aria-live="polite" aria-busy={running}>
            {events.length === 0 ? <div className="empty-state">Enter a transaction to inspect its live workflow.</div> : <ol className="trace-list">{events.map((item) => <li className={'trace-item status-' + item.status} key={item.event_id}><span className="step-index" aria-hidden="true">{item.step_number}</span><div className="trace-copy"><div className="trace-meta"><span className="event-type">{readableName(item.event_type)}</span><span className="status-text">{statusLabel(item.status)}</span></div><strong>{item.summary}</strong><span className="step-name">{readableName(item.step_name)}</span></div></li>)}</ol>}
          </div>
        </div>
        <aside className="result-column" aria-live="polite"><p className="eyebrow">RESOLUTION</p><h2>Outcome</h2>{resolution ? <div className="outcome"><div className="outcome-status"><span className="outcome-mark" aria-hidden="true">OK</span><div><span className="outcome-label">{readableName(resolution.status)}</span><strong>{readableName(resolution.action)}</strong></div></div><p>{resolution.explanation}</p><dl><div><dt>Transaction</dt><dd>{resolution.transaction_id ?? resolution.txn_id}</dd></div><div><dt>Trace steps</dt><dd>{events.length}</dd></div></dl></div> : <div className="outcome-placeholder">The completed decision will appear here.</div>}</aside>
      </section>
    </main>
  )
}

export function App(): ReactElement {
  const [showIntro, setShowIntro] = useState(true)
  const [session, setSession] = useState<AuthSession | null>(null)
  const [authLoading, setAuthLoading] = useState(authConfigured)

  useEffect(() => {
    const timer = window.setTimeout(() => setShowIntro(false), INTRO_DURATION_MS)
    return () => window.clearTimeout(timer)
  }, [])

  useEffect(() => {
    if (!supabase) {
      return undefined
    }
    let mounted = true
    void supabase.auth.getSession().then(({ data }) => {
      if (mounted) {
        setSession(data.session)
        setAuthLoading(false)
      }
    })
    const { data: listener } = supabase.auth.onAuthStateChange((_event, nextSession) => {
      setSession(nextSession)
      setAuthLoading(false)
    })
    return () => {
      mounted = false
      listener.subscription.unsubscribe()
    }
  }, [])

  if (showIntro) return <IntroScreen />
  if (authLoading) return <main className="auth-shell"><p>Restoring your session...</p></main>

  // With Supabase unconfigured the local auth-disabled development flow stays
  // usable: the operations dashboard is reachable without a session.
  const authenticated = !authConfigured || session !== null

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/sign-up" element={authenticated ? <Navigate to="/" replace /> : <SignUpPage />} />
        <Route path="/sign-in" element={authenticated ? <Navigate to="/" replace /> : <SignInPage />} />
        <Route path="/" element={authenticated ? <ResolutionApp session={session} /> : <Navigate to="/sign-up" replace />} />
        <Route path="*" element={<Navigate to={authenticated ? '/' : '/sign-up'} replace />} />
      </Routes>
    </BrowserRouter>
  )
}

export default App
