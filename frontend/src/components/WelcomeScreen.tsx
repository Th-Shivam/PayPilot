import { ArrowRight, CircleCheck, ShieldCheck, Sparkles } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { PayPilotMark } from './PayPilotMark'

export function WelcomeScreen() {
  const navigate = useNavigate()

  return (
    <main className="welcome-screen">
      <div className="welcome-grid" aria-hidden="true" />
      <header className="welcome-header">
        <a className="welcome-brand" href="/" aria-label="PayPilot home">
          <PayPilotMark />
          <span>PayPilot</span>
        </a>
        <span className="welcome-status"><span /> Operations console</span>
      </header>

      <section className="welcome-content">
        <div className="welcome-kicker"><span /> Evidence-first reconciliation</div>
        <h1>Welcome to your<br /><em>operations desk.</em></h1>
        <p>Investigate payment exceptions, follow the evidence trail, and keep every ticket moving from one focused workspace.</p>
        <button type="button" className="welcome-cta" onClick={() => navigate('/dashboard')}>
          Open dashboard <ArrowRight size={17} aria-hidden="true" />
        </button>
        <div className="welcome-points">
          <span><ShieldCheck size={15} /> Grounded in source records</span>
          <span><CircleCheck size={15} /> Built for fast decisions</span>
          <span><Sparkles size={15} /> PayPilot Agent ready</span>
        </div>
      </section>

      <footer className="welcome-footer"><span>PAYPILOT / OPERATIONS</span><span>© 2026 PayPilot Inc.</span></footer>
    </main>
  )
}
