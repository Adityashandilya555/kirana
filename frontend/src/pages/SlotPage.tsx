import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import ChatBubble, { TypingBubble } from '../components/ChatBubble'
import OfferCard from '../components/OfferCard'
import { ApiError, apiPost } from '../lib/api'
import type { ChatReply, Offer, SessionPayload, Turn } from '../lib/api'
import { openCheckout, pollUntilSettled } from '../lib/razorpay'
import type { AcceptedOrder } from '../lib/razorpay'

/**
 * The customer's whole experience: scan a sticker, land here, haggle.
 *
 * Layout notes that are load-bearing on Android Chrome. The keyboard resizes
 * only the VISUAL viewport (Chrome 108+), so `100dvh` alone does not keep the
 * composer on screen. What works is the combination already in index.css --
 * a flex column, `min-height:0` on the scroll pane so it may shrink below its
 * content, and no `position:fixed` anywhere -- plus
 * `interactive-widget=resizes-content` in the viewport meta.
 *
 * The offer card is rendered from the `offer` object the backend built out of
 * bounds.Decision. It is never parsed out of the assistant's sentence: if the
 * prose and the gate ever disagree, the number the shopper can act on is the
 * gate's.
 */
/** Where this device remembers the shopper's number, so a reload during a
 *  haggle does not stop to ask again. Their own number, their own phone. */
const PHONE_KEY = 'kirana.phone'

export default function SlotPage() {
  const { token } = useParams<{ token: string }>()
  const [session, setSession] = useState<SessionPayload | null>(null)
  const [turns, setTurns] = useState<Turn[]>([])
  const [offer, setOffer] = useState<Offer | null>(null)
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [fatal, setFatal] = useState<{ code: string; message: string } | null>(null)
  const [manual, setManual] = useState('')
  const [paying, setPaying] = useState(false)
  const [phone, setPhone] = useState('')
  const [started, setStarted] = useState(false)
  const navigate = useNavigate()

  const scrollRef = useRef<HTMLDivElement>(null)
  const endRef = useRef<HTMLDivElement>(null)

  const open = useCallback(async (slotToken: string, phoneNumber: string) => {
    setFatal(null)
    try {
      const payload = await apiPost<SessionPayload>('/api/v1/sessions', {
        slot_token: slotToken,
        // Omitted entirely when skipped, so the backend can tell "declined"
        // from "typed something unreadable".
        ...(phoneNumber.trim() ? { phone: phoneNumber.trim() } : {}),
      })
      setSession(payload)
      setTurns(payload.transcript ?? [])
    } catch (e) {
      if (e instanceof ApiError) setFatal({ code: e.code, message: e.message })
      else
        setFatal({
          code: 'NETWORK',
          message:
            'Could not reach the shop. Check your connection and try again.',
        })
    }
  }, [])

  /*
   * The number is asked once, before the chat exists, and remembered on this
   * device so a reload does not interrogate someone mid-haggle. It is kept
   * here and nowhere else -- it goes to the shop to identify a returning
   * customer and is never shown back, never put in the transcript, and never
   * reaches the model.
   */
  useEffect(() => {
    if (!token || started) return
    let remembered = ''
    try {
      remembered = localStorage.getItem(PHONE_KEY) ?? ''
    } catch {
      // Private browsing throws on access. A shopper who cannot be remembered
      // is simply asked, which is the same as a first visit.
    }
    if (remembered) {
      setPhone(remembered)
      setStarted(true)
      void open(token, remembered)
    }
  }, [token, started, open])

  function beginWith(value: string) {
    if (!token) return
    const trimmed = value.trim()
    if (trimmed) {
      try {
        localStorage.setItem(PHONE_KEY, trimmed)
      } catch {
        // Not being able to remember is not a reason to refuse to start.
      }
    }
    setStarted(true)
    void open(token, trimmed)
  }

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end', behavior: 'smooth' })
  }, [turns, sending, offer])

  async function send() {
    const text = draft.trim()
    if (!text || sending || !session) return
    setDraft('')
    setTurns((t) => [...t, { role: 'user', content: text }])
    setSending(true)
    try {
      const reply = await apiPost<ChatReply>(
        `/api/v1/sessions/${session.session_id}/chat`,
        { message: text },
      )
      setTurns((t) => [...t, { role: 'assistant', content: reply.reply }])
      if (reply.offer) setOffer(reply.offer)
    } catch (e) {
      const message =
        e instanceof ApiError
          ? e.message
          : 'That did not go through. Try once more.'
      setTurns((t) => [...t, { role: 'system', content: message }])
    } finally {
      setSending(false)
    }
  }

  /**
   * Accept → checkout → settle → redemption screen.
   *
   * Every exit is handled, not just the happy one: a dismissed sheet and a
   * failed card both release the reservation, or the discount stays held for
   * the rest of the demo. If the checkout handler succeeds but `confirm` does
   * not come back (a dropped callback), polling takes over — the webhook and
   * the poll both settle through the same plpgsql function, so whichever wins
   * produces the same row.
   */
  async function pay() {
    if (!offer || !session || paying) return
    setPaying(true)
    const note = (content: string) =>
      setTurns((t) => [...t, { role: 'system', content }])
    try {
      const order = await apiPost<AcceptedOrder>(
        `/api/v1/sessions/${session.session_id}/accept`,
        { sku: offer.sku, qty: offer.qty, discount_bps: offer.granted_bps },
      )

      if (order.stub || !order.key_id) {
        note('Payments are not switched on for this shop yet.')
        return
      }

      const outcome = await openCheckout(order, session.merchant.name)

      if ('dismissed' in outcome) {
        await apiPost(
          `/api/v1/payments/${order.order_id}/release?session_id=${encodeURIComponent(session.session_id)}`,
          {},
        ).catch(() => {})
        note('Checkout closed. Your offer is still open.')
        return
      }
      if ('failed' in outcome) {
        await apiPost(
          `/api/v1/payments/${order.order_id}/release?session_id=${encodeURIComponent(session.session_id)}`,
          {},
        ).catch(() => {})
        note(`${outcome.failed} Your offer is still open.`)
        return
      }

      let settled: Record<string, unknown> | null = null
      try {
        settled = await apiPost<Record<string, unknown>>('/api/v1/payments/confirm', {
          order_id: outcome.order_id,
          payment_id: outcome.payment_id,
          signature: outcome.signature,
        })
      } catch {
        note('Payment received — confirming with the shop…')
        settled = await pollUntilSettled(order.order_id, session.session_id)
      }

      const token = settled?.redemption_token as string | undefined
      if (token) navigate(`/r/${token}`)
      else note('Paid, but the receipt is taking a moment. Do not pay again.')
    } catch (e) {
      note(
        e instanceof ApiError
          ? e.message
          : 'Could not open checkout. Please try again.',
      )
    } finally {
      setPaying(false)
    }
  }

  // -- the code is not one of ours -----------------------------------------
  if (fatal) {
    const used = fatal.code === 'SLOT_NOT_OPEN'
    return (
      <main className="mx-auto flex h-dvh max-w-md flex-col justify-center gap-4 p-6">
        <h1 className="text-lg font-semibold text-ink">
          {fatal.code === 'NETWORK'
            ? 'No connection'
            : used
              ? 'This code has been used'
              : 'Check that code'}
        </h1>
        <p className="text-sm leading-relaxed text-ink-soft">{fatal.message}</p>
        <form
          onSubmit={(e) => {
            e.preventDefault()
            const t = manual.trim().toUpperCase()
            // Retyping a code keeps whoever we already know is holding the
            // phone; this is a wrong-sticker recovery, not a new shopper.
            if (t) void open(t, phone)
          }}
          className="flex gap-2"
        >
          <input
            value={manual}
            onChange={(e) => setManual(e.target.value)}
            placeholder="Type the code"
            autoCapitalize="characters"
            autoCorrect="off"
            spellCheck={false}
            className="min-w-0 flex-1 rounded-xl border border-hairline px-3 py-2.5
                       font-mono text-base uppercase tracking-wider"
          />
          <button
            type="submit"
            className="rounded-xl bg-accent px-4 py-2.5 text-half font-semibold text-white transition-colors hover:bg-accent-strong"
          >
            Go
          </button>
        </form>
        {/* A quiet exit, so it does not compete with Send above it. The accent
            is readable as type now, which is why the hover lands on it. */}
        <a
          href="/"
          className="text-half text-ink-soft underline underline-offset-2 transition-colors hover:text-accent"
        >
          Back to the start
        </a>
        {/* A structured error rather than a network failure is itself evidence
            the deep link, CORS and the backend are all healthy -- useful when
            debugging from the phone, so it stays, just quietly. */}
        <p className="text-[11px] text-ink-soft/70">
          Reached the shop’s server ({fatal.code}).
        </p>
      </main>
    )
  }

  /*
   * The doorstep. Asked once, before the assistant exists, because a shop
   * that recognises its regulars has to know who is at the counter before it
   * starts quoting prices -- and because asking mid-haggle would read as a
   * price that depends on whether you hand over your number.
   *
   * Skipping is a first-class answer, not a dark pattern to be nagged past.
   * A shopper who declines still haggles; they are simply treated as new.
   */
  if (!started) {
    return (
      <main className="mx-auto flex h-dvh max-w-md flex-col justify-center gap-6 p-6">
        <div>
          <h1 className="font-display text-[26px] font-bold leading-tight tracking-[-0.02em] text-ink">
            Before we start
          </h1>
          <p className="mt-2 text-half leading-relaxed text-ink-soft">
            Your number lets the shop recognise you. Regulars here get better
            prices than first-time visitors.
          </p>
        </div>

        <form
          onSubmit={(e) => {
            e.preventDefault()
            beginWith(phone)
          }}
          className="space-y-3"
        >
          <label htmlFor="phone" className="block text-half font-semibold text-ink">
            Mobile number
          </label>
          <input
            id="phone"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            placeholder="98765 43210"
            type="tel"
            inputMode="numeric"
            autoComplete="tel"
            className="w-full rounded-xl border border-hairline bg-card px-3.5 py-3 font-mono text-base tracking-wide text-ink outline-none transition-colors focus:border-accent"
          />
          <button
            type="submit"
            disabled={!phone.trim()}
            className="w-full rounded-xl bg-accent px-4 py-3 text-half font-semibold text-white transition-colors hover:bg-accent-strong disabled:bg-sunk disabled:text-ink-faint"
          >
            Start
          </button>
        </form>

        <button
          type="button"
          onClick={() => beginWith('')}
          className="text-half text-ink-soft underline underline-offset-2 transition-colors hover:text-accent"
        >
          Skip — just show me the price
        </button>

        <p className="text-tiny leading-relaxed text-ink-faint">
          Used only to recognise you at this shop. It is never shown to the
          assistant you are about to talk to.
        </p>
      </main>
    )
  }

  if (!session) {
    return (
      <main className="flex h-dvh items-center justify-center">
        <p className="text-sm text-ink-soft">Opening…</p>
      </main>
    )
  }

  const itemName =
    session.catalog.find((c) => c.sku === offer?.sku)?.name ?? offer?.sku
  const turnsLeft = (session.campaign.max_turns ?? 6) - turns.filter((t) => t.role === 'user').length

  return (
    <div className="chat-shell mx-auto max-w-md bg-surface">
      <header className="flex-none border-b border-hairline bg-card px-4 py-3">
        <h1 className="font-display text-base font-medium leading-tight text-ink">
          {session.merchant.name}
        </h1>
        <p className="mt-0.5 text-tiny text-ink-soft">
          {session.merchant.store_line} · code{' '}
          <span className="font-mono">{session.slot.slot_token}</span>
        </p>
      </header>

      <div ref={scrollRef} className="chat-scroll space-y-3 px-4 py-4">
        {turns.length === 0 && (
          <ChatBubble role="assistant">
            Namaste! Ask me about anything on the shelf and I will see what I can
            do on the price.
          </ChatBubble>
        )}
        {turns.map((t, i) => (
          <ChatBubble key={i} role={t.role}>
            {t.content}
          </ChatBubble>
        ))}
        {sending && <TypingBubble />}
        {offer && (
          <div className="pt-1">
            <OfferCard
              offer={offer}
              itemName={itemName}
              onAccept={() => void pay()}
              accepting={paying}
            />
          </div>
        )}
        <div ref={endRef} />
      </div>

      <form
        className="chat-composer border-t border-hairline bg-card px-3 py-2.5"
        onSubmit={(e) => {
          e.preventDefault()
          void send()
        }}
      >
        <div className="flex items-end gap-2">
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={turnsLeft > 0 ? 'Ask about a price…' : 'Final price reached'}
            enterKeyHint="send"
            /* text-base, not smaller: iOS Safari zooms the viewport on focus
               for anything under 16px, which throws the composer off screen. */
            className="min-w-0 flex-1 rounded-full border-[1.5px] border-hairline bg-surface
                       px-4 py-2.5 text-base outline-none transition-colors
                       focus:border-accent-strong"
          />
          <button
            type="submit"
            disabled={sending || !draft.trim()}
            /* Accent, now that it can be one: Z-Matrix slate carries white at
               5.9:1, so the send button is the brand colour rather than the
               near-black it had to be under the old turquoise. */
            className="shrink-0 rounded-full bg-accent px-5 py-2.5 text-half font-semibold
                       text-white transition-colors hover:bg-accent-strong
                       disabled:opacity-40"
          >
            Send
          </button>
        </div>
      </form>
    </div>
  )
}
