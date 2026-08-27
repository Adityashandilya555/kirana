import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

/**
 * The front door.
 *
 * This exists because the catch-all route used to redirect to /s/PHASE0TEST,
 * a Phase 0 diagnostic placeholder that was never a real slot. Once SlotPage
 * became the real chat, the bare domain opened a session for a token that does
 * not exist and greeted every visitor with "That code did not work" — an error
 * screen as the front page, which is a bad thing to have on a projector.
 *
 * Nobody reaches this page in the real flow: a customer scans a sticker and
 * deep-links straight to /s/<token>. It is here for the person who types the
 * domain, and for the merchant who needs a way into the console.
 */
export default function LandingPage() {
  const [code, setCode] = useState('')
  const navigate = useNavigate()

  return (
    <main className="mx-auto flex min-h-dvh max-w-md flex-col justify-center gap-8 p-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink">
          Kirana Agent
        </h1>
        <p className="mt-2 text-sm leading-relaxed text-ink-soft">
          Scan the code on a shelf sticker to haggle with the shopkeeper’s
          assistant. Every discount it offers is inside a limit the shop
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
        <label htmlFor="code" className="block text-sm font-medium text-ink">
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
            className="min-w-0 flex-1 rounded-xl border border-hairline px-3.5 py-3
                       font-mono text-base uppercase tracking-widest outline-none
                       focus:border-accent"
          />
          <button
            type="submit"
            disabled={!code.trim()}
            className="rounded-xl bg-accent px-5 py-3 text-sm font-semibold text-white
                       disabled:opacity-40"
          >
            Go
          </button>
        </div>
      </form>

      <nav className="space-y-2 border-t border-hairline pt-5 text-sm">
        <p className="text-xs font-medium uppercase tracking-wide text-ink-soft">
          For the shopkeeper
        </p>
        <a
          href="/verify"
          className="block rounded-xl border border-hairline px-3.5 py-3 text-ink
                     active:bg-slate-50"
        >
          Verify a redemption code →
        </a>
        <p className="pt-1 text-xs leading-relaxed text-ink-soft">
          The live audit console is at <code className="font-mono">/merchant/</code>
          followed by the campaign id.
        </p>
      </nav>
    </main>
  )
}
