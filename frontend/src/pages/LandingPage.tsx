import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Button, inputClass } from '../components/ui'

/**
 * The front door.
 *
 * Nobody reaches this in the real flow — a customer scans a sticker and deep
 * links straight to /s/<token>. It exists for the person who types the domain,
 * and for the shopkeeper who needs a way into the console. It used to redirect
 * to a placeholder token left over from early development, which meant the
 * bare domain greeted every
 * visitor with an error screen.
 */
export default function LandingPage() {
  const [code, setCode] = useState('')
  const navigate = useNavigate()

  return (
    <main className="mx-auto flex min-h-dvh max-w-md flex-col justify-center gap-10 p-6">
      <header>
        <span
          aria-hidden
          className="mb-5 grid h-11 w-11 place-items-center rounded-xl bg-accent shadow-card"
        >
          <svg width="22" height="22" viewBox="0 0 16 16" fill="none">
            <rect x="2" y="3" width="12" height="2.4" rx="1.2" fill="#ffffff" />
            <rect x="2" y="7" width="9" height="2.4" rx="1.2" fill="#ffffff" opacity="0.7" />
            <rect x="2" y="11" width="6" height="2.4" rx="1.2" fill="#ffffff" opacity="0.4" />
          </svg>
        </span>
        <h1 className="font-display text-[34px] font-bold leading-[1.1] tracking-[-0.02em] text-ink">
          Kirana Agent
        </h1>
        <p className="mt-3 text-half leading-relaxed text-ink-soft">
          Scan the code on a shelf sticker to haggle with the shopkeeper’s
          assistant. Every discount it offers sits inside a limit the shop
          committed to before you arrived.
        </p>
      </header>

      <form
        onSubmit={(e) => {
          e.preventDefault()
          const t = code.trim().toUpperCase()
          if (t) navigate(`/s/${t}`)
        }}
        className="space-y-2"
      >
        <label htmlFor="code" className="block text-half font-semibold text-ink">
          Have a code from a sticker?
        </label>
        <div className="flex gap-2">
          <input
            id="code"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="ABCD1234EF"
            autoCapitalize="characters"
            autoCorrect="off"
            spellCheck={false}
            className={`${inputClass} bg-card font-mono uppercase tracking-[0.12em]`}
          />
          <Button type="submit" disabled={!code.trim()}>
            Go
          </Button>
        </div>
      </form>

      <nav className="space-y-2 border-t border-hairline pt-6">
        <p className="text-2xs font-semibold uppercase tracking-[0.08em] text-ink-soft">
          For the shopkeeper
        </p>
        <a
          href="/merchant"
          className="block rounded-xl border border-hairline bg-card px-4 py-3 text-half text-ink transition-colors hover:bg-sunk"
        >
          Open the console →
        </a>
        <a
          href="/verify"
          className="block rounded-xl border border-hairline bg-card px-4 py-3 text-half text-ink transition-colors hover:bg-sunk"
        >
          Verify a redemption code →
        </a>
      </nav>
    </main>
  )
}
