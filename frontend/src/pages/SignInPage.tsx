import { useState, useId, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowRight, Eye, EyeOff } from 'lucide-react'
import { AuthLayout } from '../components/auth/AuthLayout'

export function SignInPage() {
  const navigate = useNavigate()
  const [status, setStatus] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const emailId = useId()
  const passwordId = useId()

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setStatus('Welcome back. Workspace ready.')
    window.setTimeout(() => navigate('/dashboard'), 250)
  }

  return (
    <AuthLayout mode="signin">
      <div className="mb-8 text-left">
        <p className="mb-2 text-blue-600 font-mono text-[11px] font-semibold tracking-widest uppercase">
          Welcome Back
        </p>
        <h1 className="mb-3 text-slate-900 font-sans text-3xl sm:text-[34px] md:text-[36px] font-semibold tracking-tight leading-[1.15]">
          Welcome back.
        </h1>
        <p className="text-slate-500 text-sm leading-relaxed max-w-[360px] m-0">
          Continue investigating what happened to your payments.
        </p>
      </div>

      <form className="flex flex-col gap-4 text-left" onSubmit={handleSubmit}>
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
          <div className="flex items-center justify-between">
            <label htmlFor={passwordId} className="text-slate-700 text-xs font-semibold tracking-tight">
              Password
            </label>
            <button
              type="button"
              className="p-0 border-none bg-transparent text-blue-600 font-sans text-[13px] font-medium cursor-pointer hover:underline"
              onClick={() => setStatus('Password reset link requested.')}
            >
              Forgot password?
            </button>
          </div>
          <div className="relative flex items-center w-full">
            <input
              id={passwordId}
              name="password"
              type={showPassword ? 'text' : 'password'}
              placeholder="Enter your password"
              autoComplete="current-password"
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

        <button
          type="submit"
          className="inline-flex items-center justify-center gap-2 w-full h-11 mt-2 rounded-[8px] bg-blue-600 text-white font-sans text-sm font-semibold cursor-pointer hover:bg-blue-700 transition-all duration-150 active:scale-[0.99]"
        >
          <span>Sign in</span>
          <ArrowRight size={15} aria-hidden="true" />
        </button>

        {status && <p className="m-0 text-blue-600 text-xs text-center font-medium" role="status">{status}</p>}
      </form>

      <div className="mt-6 text-slate-500 text-[13px] text-left">
        <span>Don't have a PayPilot workspace?</span>{' '}
        <button
          type="button"
          className="p-0 border-none bg-transparent text-blue-600 font-sans text-[13px] font-semibold cursor-pointer hover:underline ml-1"
          onClick={() => navigate('/sign-up')}
        >
          Create one →
        </button>
      </div>
    </AuthLayout>
  )
}
