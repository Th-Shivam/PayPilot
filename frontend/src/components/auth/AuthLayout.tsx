import { type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { PayPilotMark } from './PayPilotMark'
import bgImage from '../../assets/bg-sign-in-sign-up.png'

interface AuthLayoutProps {
  mode: 'signup' | 'signin'
  children: ReactNode
}

export function AuthLayout({ children }: AuthLayoutProps) {
  return (
    <main className="relative flex flex-col justify-between w-full h-screen max-h-screen overflow-hidden isolate bg-[#fbfaf8] text-slate-900 max-md:h-auto max-md:min-h-screen max-md:max-h-none max-md:overflow-y-auto select-none">
      {/* Background layer: Using object-cover to let the illustration fill the screen */}
      <div className="absolute inset-0 -z-10 overflow-hidden pointer-events-none" aria-hidden="true">
        <img
          src={bgImage}
          alt="PayPilot Visual Environment"
          className="absolute inset-0 w-full h-full bg-cover object-center select-none"
        />
        {/* Intentionally removing gradient overlays as requested to keep the art clean */}
      </div>

      {/* Viewport Header */}
      <header className="relative z-10 flex items-center justify-between pt-8 px-8 sm:px-14 md:px-20 max-md:pt-6 max-md:px-6">
        <Link className="inline-flex items-center gap-2.5 text-slate-900 font-sans text-lg font-semibold tracking-tight no-underline group" to="/sign-up" aria-label="PayPilot home">
          <PayPilotMark />
          <span className="group-hover:text-blue-600 transition-colors">PayPilot</span>
        </Link>
      </header>

      {/* Main Composition */}
      <div className="relative z-10 flex flex-1 w-full h-full items-center max-md:py-8 max-md:px-6">
        {/* Left Zone: Form shifted toward center (~420px max-width) */}
        <div className="flex flex-col justify-center w-1/2 flex-[0_0_50%] pl-8 sm:pl-16 md:pl-24 lg:pl-32 pr-6 box-border max-lg:w-[55%] max-lg:flex-[0_0_55%] max-lg:pl-14 max-md:w-full max-md:flex-[1_0_auto] max-md:p-0 text-left">
          <div className="w-full max-w-[420px] max-md:max-w-full text-left">{children}</div>
        </div>

        {/* Right Zone */}
        <div className="flex-1 h-full pointer-events-none max-md:hidden" aria-hidden="true" />
      </div>

      {/* Viewport Footer */}
      <footer className="relative z-10 flex items-center justify-between pb-8 px-8 sm:px-14 md:px-20 text-slate-400 font-mono text-[10px] tracking-wider uppercase max-md:p-6 max-md:px-6">
        <span>© 2026 PayPilot Inc. All rights reserved.</span>
        <div className="flex gap-4">
          <a href="#" className="text-inherit no-underline hover:text-slate-600 transition-colors">Privacy</a>
          <a href="#" className="text-inherit no-underline hover:text-slate-600 transition-colors">Terms</a>
        </div>
      </footer>
    </main>
  )
}
