import { useCallback, useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import ChatBubble, { TypingBubble } from '../components/ChatBubble'
import OfferCard from '../components/OfferCard'
import { ApiError, apiPost } from '../lib/api'
import type { ChatReply, Offer, SessionPayload, Turn } from '../lib/api'

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
export default function SlotPage() {
  const { token } = useParams<{ token: string }>()
  const [session, setSession] = useState<SessionPayload | null>(null)
  const [turns, setTurns] = useState<Turn[]>([])
  const [offer, setOffer] = useState<Offer | null>(null)
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [fatal, setFatal] = useState<{ code: string; message: string } | null>(null)
  const [manual, setManual] = useState('')

  const scrollRef = useRef<HTMLDivElement>(null)
  const endRef = useRef<HTMLDivElement>(null)

  const open = useCallback(async (slotToken: string) => {
    setFatal(null)
    try {
      const payload = await apiPost<SessionPayload>('/api/v1/sessions', {
        slot_token: slotToken,
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

  useEffect(() => {
    if (token) void open(token)
  }, [token, open])

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

  // -- the code is not one of ours -----------------------------------------
  if (fatal) {
    return (
      <main className="mx-auto flex h-dvh max-w-md flex-col justify-center gap-4 p-6">
        <h1 className="text-lg font-semibold text-ink">
          {fatal.code === 'NETWORK' ? 'No connection' : 'That code did not work'}
        </h1>
        <p className="text-sm leading-relaxed text-ink-soft">{fatal.message}</p>
        <form
          onSubmit={(e) => {
            e.preventDefault()
            const t = manual.trim().toUpperCase()
            if (t) void open(t)
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
            className="rounded-xl bg-accent px-4 py-2.5 text-sm font-semibold text-white"
          >
            Go
          </button>
        </form>
        {/* A structured error rather than a network failure is itself
            evidence the deep link, CORS and the backend are all healthy. */}
        <p className="text-xs text-ink-soft">
          Reached the shop’s server ({fatal.code}).
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
    <div className="chat-shell mx-auto max-w-md bg-slate-50">
      <header className="flex-none border-b border-hairline bg-white px-4 py-3">
        <h1 className="text-sm font-semibold text-ink">{session.merchant.name}</h1>
        <p className="text-xs text-ink-soft">
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
            <OfferCard offer={offer} itemName={itemName} />
          </div>
        )}
        <div ref={endRef} />
      </div>

      <form
        className="chat-composer border-t border-hairline bg-white px-3 py-2.5"
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
            className="min-w-0 flex-1 rounded-full border border-hairline px-4 py-2.5
                       text-base outline-none focus:border-accent"
          />
          <button
            type="submit"
            disabled={sending || !draft.trim()}
            className="shrink-0 rounded-full bg-accent px-4 py-2.5 text-sm font-semibold
                       text-white disabled:opacity-40"
          >
            Send
          </button>
        </div>
      </form>
    </div>
  )
}
