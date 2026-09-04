import { useState } from 'react'
import { supabase } from '../../lib/supabase'

interface GoogleButtonProps {
  /** Button text; differs slightly between sign-in and sign-up. */
  label?: string
  /** Surface OAuth errors in the host page's existing error slot. */
  onError?: (message: string) => void
  /** Let the host disable the button while another auth action is in flight. */
  disabled?: boolean
}

/** Google's four-colour "G" mark, inline so there is no asset dependency. */
function GoogleMark(): React.ReactElement {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
      <path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.92c1.71-1.57 2.68-3.89 2.68-6.62Z" />
      <path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.92-2.26c-.81.54-1.84.86-3.04.86-2.34 0-4.32-1.58-5.03-3.7H.96v2.33A9 9 0 0 0 9 18Z" />
      <path fill="#FBBC05" d="M3.97 10.72a5.41 5.41 0 0 1 0-3.44V4.95H.96a9 9 0 0 0 0 8.1l3.01-2.33Z" />
      <path fill="#EA4335" d="M9 3.58c1.32 0 2.5.45 3.44 1.35l2.58-2.58C13.46.89 11.43 0 9 0A9 9 0 0 0 .96 4.95l3.01 2.33C4.68 5.16 6.66 3.58 9 3.58Z" />
    </svg>
  )
}

/**
 * "Continue with Google" using Supabase OAuth. On success the browser is
 * redirected to Google and returns to the app origin, where the session
 * listener in App picks up the new session (detectSessionInUrl is enabled).
 */
export function GoogleButton({ label = 'Continue with Google', onError, disabled }: GoogleButtonProps): React.ReactElement {
  const [busy, setBusy] = useState(false)

  const handleClick = async (): Promise<void> => {
    if (busy) return
    if (!supabase) {
      // Keeps the credential-free local preview flow honest instead of failing.
      onError?.('Google sign-in needs Supabase configuration (VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY).')
      return
    }
    setBusy(true)
    const { error } = await supabase.auth.signInWithOAuth({
      provider: 'google',
      options: { redirectTo: window.location.origin },
    })
    if (error) {
      onError?.(error.message)
      setBusy(false)
    }
    // On success a redirect is underway; leaving busy set avoids a flicker.
  }

  return (
    <button
      type="button"
      onClick={() => { void handleClick() }}
      disabled={disabled || busy}
      className="inline-flex items-center justify-center gap-2.5 w-full h-11 rounded-[8px] border border-slate-200 bg-white text-slate-700 font-sans text-sm font-semibold cursor-pointer hover:bg-slate-50 transition-all duration-150 active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-70 disabled:active:scale-100"
    >
      <GoogleMark />
      <span>{busy ? 'Redirecting to Google...' : label}</span>
    </button>
  )
}

/** "or" divider shown between the Google button and the email form. */
export function AuthDivider(): React.ReactElement {
  return (
    <div className="flex items-center gap-3 my-5" aria-hidden="true">
      <span className="h-px flex-1 bg-slate-200" />
      <span className="text-slate-400 font-sans text-[11px] font-medium uppercase tracking-wider">or</span>
      <span className="h-px flex-1 bg-slate-200" />
    </div>
  )
}
