import { useState, useId, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowRight, Eye, EyeOff } from 'lucide-react'
import { AuthLayout } from '../components/auth/AuthLayout'
import { supabase } from '../lib/supabase'

export function SignUpPage() {
  const navigate = useNavigate()
  const [status, setStatus] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [showPassword, setShowPassword] = useState(false)
  const nameId = useId()
  const emailId = useId()
  const passwordId = useId()

  const handleSubmit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault()
    if (busy) return
    const fields = new FormData(event.currentTarget)
    const fullName = String(fields.get('name') ?? '')
    const email = String(fields.get('email') ?? '')
    const password = String(fields.get('password') ?? '')

    setError('')
    if (!supabase) {
      // Auth is not configured locally, so keep the credential-free preview flow.
      setStatus('Workspace details captured. Ready to authenticate.')
      window.setTimeout(() => navigate('/dashboard'), 250)
      return
    }

    setBusy(true)
    setStatus('')
    const { data, error: signUpError } = await supabase.auth.signUp({
      email,
      password,
      options: { data: { full_name: fullName } },
    })
    if (signUpError) {
      setError(signUpError.message)
    } else if (!data.session) {
      setStatus('Check your email to confirm the account, then sign in.')
    } else {
      // Confirmations are disabled, so the session listener in App takes over.
      setStatus('Workspace ready.')
    }
    setBusy(false)
  }

  return (
    <AuthLayout mode="signup">
      <div className="mb-8 text-left">
        <p className="mb-2 text-blue-600 font-mono text-[11px] font-semibold tracking-widest uppercase">
          Get Started
        </p>
        <h1 className="mb-3 text-slate-900 font-sans text-3xl sm:text-[34px] md:text-[36px] font-semibold tracking-tight leading-[1.15]">
          Create your PayPilot
          <br />
          workspace.
        </h1>
        <p className="text-slate-500 text-sm leading-relaxed max-w-[360px] m-0">
          Investigate settlements faster, with every answer grounded in the underlying records.
        </p>
      </div>

      <form className="flex flex-col gap-4 text-left" onSubmit={handleSubmit}>
        <div className="flex flex-col gap-1.5 text-left">
          <label htmlFor={nameId} className="text-slate-700 text-xs font-semibold tracking-tight">
            Full name
          </label>
          <input
            id={nameId}
            name="name"
            type="text"
            placeholder="Alex Morgan"
            autoComplete="name"
            className="w-full h-11 px-3.5 box-border border border-slate-200 rounded-[8px] bg-white text-slate-900 font-sans text-sm outline-none placeholder:text-slate-400 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-all duration-150"
            required
          />
        </div>

        <div className="flex flex-col gap-1.5 text-left">
          <label htmlFor={emailId} className="text-slate-700 text-xs font-semibold tracking-tight">
            Work email
          </label>
          <input
            id={emailId}
            name="email"
            type="email"
            placeholder="you@company.com"
            autoComplete="email"
            className="w-full h-11 px-3.5 box-border border border-slate-200 rounded-[8px] bg-white text-slate-900 font-sans text-sm outline-none placeholder:text-slate-400 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-all duration-150"
            required
          />
        </div>

        <div className="flex flex-col gap-1.5 text-left">
          <label htmlFor={passwordId} className="text-slate-700 text-xs font-semibold tracking-tight">
            Password
          </label>
          <div className="relative flex items-center w-full">
            <input
              id={passwordId}
              name="password"
              type={showPassword ? 'text' : 'password'}
              placeholder="Min. 8 characters"
              autoComplete="new-password"
              minLength={8}
              className="w-full h-11 pl-3.5 pr-10 box-border border border-slate-200 rounded-[8px] bg-white text-slate-900 font-sans text-sm outline-none placeholder:text-slate-400 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-all duration-150"
              required
            />
            <button
              type="button"
              aria-label={showPassword ? 'Hide password' : 'Show password'}
              className="absolute right-2.5 flex items-center justify-center w-7 h-7 p-0 border-none rounded bg-transparent text-slate-400 hover:text-slate-600 transition-colors cursor-pointer"
              onClick={() => setShowPassword(!showPassword)}
            >
              {showPassword ? <EyeOff size={15} /> : <Eye size={15} />}
            </button>
          </div>
        </div>

        <label className="flex items-center gap-2.5 mt-1 text-slate-500 text-[13px] leading-snug cursor-pointer">
          <input type="checkbox" className="w-4 h-4 m-0 accent-blue-600 rounded border-slate-300 cursor-pointer" required />
          <span>
            I agree to the <a href="#" className="text-blue-600 font-medium no-underline hover:underline">Terms</a> and{' '}
            <a href="#" className="text-blue-600 font-medium no-underline hover:underline">Privacy Policy</a>.
          </span>
        </label>

        <button
          type="submit"
          disabled={busy}
          className="inline-flex items-center justify-center gap-2 w-full h-11 mt-2 rounded-[8px] bg-blue-600 text-white font-sans text-sm font-semibold cursor-pointer hover:bg-blue-700 transition-all duration-150 active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-70 disabled:active:scale-100"
        >
          <span>{busy ? 'Creating workspace...' : 'Create workspace'}</span>
          <ArrowRight size={15} aria-hidden="true" />
        </button>

        {error && <p className="m-0 text-red-600 text-xs text-center font-medium" role="alert">{error}</p>}
        {status && <p className="m-0 text-blue-600 text-xs text-center font-medium" role="status">{status}</p>}
      </form>

      <div className="mt-6 text-slate-500 text-[13px] text-left">
        <span>Already have an account?</span>{' '}
        <button
          type="button"
          className="p-0 border-none bg-transparent text-blue-600 font-sans text-[13px] font-semibold cursor-pointer hover:underline ml-1"
          onClick={() => navigate('/sign-in')}
        >
          Sign in →
        </button>
      </div>
    </AuthLayout>
  )
}
