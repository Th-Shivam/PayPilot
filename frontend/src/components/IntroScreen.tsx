import { Skiper39 } from '@/components/ui/skiper-ui/skiper39'

const INTRO_DURATION_MS = 4000

export function IntroScreen() {
  return (
    <section
      className="fixed inset-0 z-[100] overflow-hidden bg-[#f8f8f6] text-[#08060d]"
      aria-label="PayPilot evidence-first assistant loading"
      role="status"
    >
      <div
        className="absolute inset-0 [&>div]:w-full [&>div]:h-full [&>div]:bg-[#f8f8f6] [&>div>div:first-child]:hidden"
        aria-hidden="true"
      >
        <Skiper39 />
      </div>
      <div className="absolute top-[clamp(84px,13vh,132px)] right-6 left-6 z-[1] flex flex-col items-center gap-4 pointer-events-none text-center">
        <p className="inline-flex items-center gap-2.5 m-0 text-[#08060d]/60 font-mono text-[11px] font-semibold tracking-[0.16em] uppercase">
          <span className="inline-block w-7 h-[1px] bg-current" aria-hidden="true" />
          Evidence-first reconciliation
        </p>
        <h1 className="max-w-[720px] m-0 text-[#08060d] font-sans text-5xl sm:text-6xl md:text-[72px] font-medium leading-[0.96] [text-wrap:balance]">
          Proof before
          <br />
          <em className="text-[#08060d]/40 not-italic">prediction.</em>
        </h1>
        <p className="max-w-[420px] m-0 text-[#08060d]/60 text-sm sm:text-base leading-normal [text-wrap:balance]">
          Every answer is grounded in transaction data, with the trail to prove it.
        </p>
        <div className="flex w-[min(300px,72vw)] justify-between mt-4 text-[#08060d]/50 font-mono text-[10px] tracking-wider uppercase">
          <span>Building your evidence trail</span>
        </div>
        <div className="w-[min(300px,72vw)] h-[2px] overflow-hidden bg-[#08060d]/20" aria-hidden="true">
          <span
            className="block w-full h-full bg-[#08060d] animate-[intro-progress_linear_forwards]"
            style={{ animationDuration: `${INTRO_DURATION_MS}ms` }}
          />
        </div>

      </div>
    </section>
  )
}
