import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import MerchantShell from '../../components/MerchantShell'
import { Card, Eyebrow, Money, Pct, Pill, Stat } from '../../components/ui'
import { getAudit, getSessionAudit, qrSheetUrl } from '../../lib/merchant'
import type { AuditRow, Campaign, SessionAudit } from '../../lib/merchant'

/**
 * The live log, grouped into conversations.
 *
 * A flat feed is what the database holds and the wrong shape for a person: the
 * interesting unit is one shopper's negotiation, not one decision. A judge
 * wants to follow a single haggle from scan to settlement and watch the gate
 * intervene inside it — which a chronological list of twelve interleaved rows
 * actively hides.
 *
 * So rows are threads, rendered as a console log: monospace, timestamped,
 * aligned. The moment that matters — the model asking for more than it may
 * have — gets its own bar rather than being one line among many.
 *
 * Polling, not SSE: Railway closes idle SSE connections at five minutes and a
 * feed that dies silently mid-demo is worse than one costing a request every
 * 1.5 seconds.
 */

const POLL_MS = 1500

/** The five ways this system fails gracefully. The track's bar asks to show
 *  one; naming all five and letting a judge click each is the stronger answer,
 *  and each carries the sentence explaining why the failure is correct. */
const FAILURES = {
  blocked: {
    label: 'Injection blocked',
    kinds: ['injection_blocked'],
    note: 'Screened before any provider was constructed. The row carries no llm_provider at all, and that null is the machine-checkable proof the model was never asked.',
  },
  clamped: {
    label: 'Over-ask clamped',
    kinds: ['clamped'],
    note: 'The model proposed more than this sticker allows. The gate granted its ceiling and told the model why, and it re-proposed inside the bound rather than arguing.',
  },
  refused: {
    label: 'Refused outright',
    kinds: ['rejected'],
    note: 'No offer was possible — the turn limit, the margin floor, or an exhausted budget. A refusal is the product working, not an error path.',
  },
  fallback: {
    label: 'Model unavailable',
    kinds: ['llm_fallback', 'llm_error'],
    note: 'Every provider failed. A deterministic responder still produced a bounded offer, so an outage costs the demo its prose and none of its guarantees.',
  },
  payment: {
    label: 'Payment trouble',
    kinds: ['payment_failed'],
    note: 'A card failed or checkout was dismissed. The reservation went back to the budget rather than holding discount for the rest of the day.',
  },
} as const

type FilterKey = 'all' | keyof typeof FAILURES

const KIND_TONE: Record<string, 'pass' | 'fail' | 'warn' | 'neutral'> = {
  approved: 'pass',
  settled: 'pass',
  verified: 'pass',
  clamped: 'warn',
  llm_fallback: 'warn',
  rejected: 'fail',
  injection_blocked: 'fail',
  verify_rejected: 'fail',
  llm_error: 'fail',
  payment_failed: 'fail',
}

const KIND_LABEL: Record<string, string> = {
  session_opened: 'scan',
  injection_blocked: 'blocked',
  tool_call: 'tool',
  proposal: 'proposal',
  approved: 'approved',
  clamped: 'capped',
  rejected: 'refused',
  llm_fallback: 'fallback',
  llm_error: 'model error',
  order_created: 'checkout',
  settled: 'paid',
  payment_failed: 'pay failed',
  verified: 'verified',
  verify_rejected: 'reuse refused',
  campaign_committed: 'committed',
}

interface Thread {
  sessionId: string
  rows: AuditRow[]
  started: string
  outcome: 'blocked' | 'settled' | 'refused' | 'offered' | 'open'
  widestAsk: { proposed: number; granted: number } | null
}

function buildThreads(rows: AuditRow[]): Thread[] {
  const map = new Map<string, AuditRow[]>()
  for (const r of rows) {
    if (!r.session_id) continue
    const list = map.get(r.session_id) ?? []
    list.push(r)
    map.set(r.session_id, list)
  }

  const threads: Thread[] = []
  for (const [sessionId, list] of map) {
    const ordered = [...list].sort((a, b) => a.id - b.id)
    const kinds = new Set(ordered.map((r) => r.kind))
    const outcome: Thread['outcome'] = kinds.has('injection_blocked')
      ? 'blocked'
      : kinds.has('settled')
        ? 'settled'
        : kinds.has('approved') || kinds.has('clamped')
          ? 'offered'
          : kinds.has('rejected')
            ? 'refused'
            : 'open'

    // A thread's headline is the widest gap the gate closed inside it.
    let widest: Thread['widestAsk'] = null
    let gap = -1
    for (const r of ordered) {
      if (r.proposed_bps != null && r.granted_bps != null) {
        const d = r.proposed_bps - r.granted_bps
        if (d > gap) {
          gap = d
          widest = { proposed: r.proposed_bps, granted: r.granted_bps }
        }
      }
    }

    threads.push({
      sessionId,
      rows: ordered,
      started: ordered[0]?.created_at ?? '',
      outcome,
      widestAsk: widest,
    })
  }
  return threads.sort((a, b) => (a.started < b.started ? 1 : -1))
}

const clockOf = (iso: string) =>
  iso ? new Date(iso).toLocaleTimeString('en-GB', { hour12: false }) : '--:--:--'

/** The clamp, drawn. What the gate let through out of what was asked — the
 *  most important thing on this screen, and it has to read across a room. */
function ClampBar({ proposed, granted }: { proposed: number; granted: number }) {
  const kept = Math.max(3, Math.min(100, (granted / Math.max(proposed, 1)) * 100))
  return (
    <div className="mt-2 max-w-md">
      <div className="flex items-baseline justify-between text-xxs text-ink-soft">
        <span>
          asked <Pct bps={proposed} className="text-ink" />
        </span>
        <span>
          granted <Pct bps={granted} className="text-pass" />
        </span>
      </div>
      <div className="mt-1 flex h-2.5 overflow-hidden rounded-full bg-fail-bg">
        <div className="bg-pass" style={{ width: `${kept}%` }} />
      </div>
    </div>
  )
}

function LogRow({ r }: { r: AuditRow }) {
  const tone = KIND_TONE[r.kind] ?? 'neutral'
  const clamped =
    r.proposed_bps != null && r.granted_bps != null && r.granted_bps < r.proposed_bps

  return (
    <li className="border-t border-hairline px-4 py-2.5 first:border-t-0 md:px-5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="font-mono text-2xs tabular-nums text-ink-soft">
          {clockOf(r.created_at)}
        </span>
        <Pill tone={tone} dot>
          {KIND_LABEL[r.kind] ?? r.kind}
        </Pill>
        <span className="font-mono text-2xs text-ink-soft">{r.code}</span>
        {r.binding_constraint && (
          <span className="text-2xs text-ink-soft">
            held by <span className="font-mono">{r.binding_constraint}</span>
          </span>
        )}
        <span className="ml-auto flex items-center gap-2 font-mono text-2xs text-ink-soft">
          {/* A null provider on a blocked row is the proof no model was
              consulted. Render it as words, never as a blank. */}
          {r.llm_provider ? (
            r.llm_provider
          ) : r.kind === 'injection_blocked' ? (
            <span className="text-fail">no model called</span>
          ) : null}
          {r.latency_ms ? <span className="tabular-nums">{r.latency_ms}ms</span> : null}
        </span>
      </div>

      {clamped && <ClampBar proposed={r.proposed_bps!} granted={r.granted_bps!} />}

      {r.raw_user_message && (
        <p className="mt-2 border-l-2 border-hairline pl-2.5 font-mono text-2xs italic text-ink-soft">
          “{r.raw_user_message}”
        </p>
      )}
      <p className="mt-1.5 text-mini leading-relaxed">{r.human_reason}</p>
    </li>
  )
}

/** The claim the binding feature rests on, made visible. */
function ScopePanel({ s }: { s: SessionAudit }) {
  const scope = s.bound_sku
    ? `one product · ${s.bound_sku}`
    : s.shelf_name
      ? `shelf · ${s.shelf_name}`
      : 'the whole shop'

  return (
    <div className="mt-3 rounded-xl border border-hairline bg-surface px-4 py-3">
      <Eyebrow>What the assistant could see</Eyebrow>
      <p className="mt-1.5 text-mini">
        Scope: <span className="font-medium">{scope}</span>{' '}
        <span className="font-mono text-2xs text-ink-soft">
          {s.visible_skus.join(' ')}
        </span>
      </p>
      {s.withheld_skus.length > 0 ? (
        <>
          <p className="mt-2 text-mini text-ink-soft">
            Never shown:{' '}
            <span className="font-mono text-2xs line-through">
              {s.withheld_skus.join(' ')}
            </span>
          </p>
          <p className="mt-1 text-2xs leading-relaxed text-ink-soft">
            {s.scope_recorded
              ? 'Not forbidden — absent. Recorded when this conversation opened, so editing a shelf since cannot change what this says.'
              : 'Not forbidden — absent. Reconstructed from today’s shelves: this conversation predates scope recording, so it may not match what the model actually saw.'}
          </p>
        </>
      ) : (
        <p className="mt-2 text-2xs text-ink-soft">
          This sticker is unbound, so nothing was withheld.
        </p>
      )}
      {!s.scope_recorded && (
        <p className="mt-2 text-2xs text-warn">
          Reconstructed, not recorded — treat as indicative rather than evidence.
        </p>
      )}
    </div>
  )
}

export default function CampaignDetailPage() {
  const { campaignId } = useParams<{ campaignId: string }>()
  const [params] = useSearchParams()
  const justCommitted = params.get('committed') === '1'

  const [campaign, setCampaign] = useState<Campaign | null>(null)
  const [rows, setRows] = useState<AuditRow[]>([])
  const [sessions, setSessions] = useState<SessionAudit[]>([])
  const [filter, setFilter] = useState<FilterKey>('all')
  const [showTools, setShowTools] = useState(false)
  const [openScope, setOpenScope] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const cursor = useRef(0)

  const poll = useCallback(async () => {
    if (!campaignId) return
    try {
      const feed = await getAudit(campaignId, cursor.current)
      setCampaign(feed.campaign)
      if (feed.items?.length) {
        cursor.current = feed.cursor
        setRows((r) => [...r, ...feed.items].slice(-1000))
        // Only refetch conversation context when something actually changed.
        getSessionAudit(campaignId).then((p) => setSessions(p.sessions)).catch(() => {})
      }
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [campaignId])

  useEffect(() => {
    void poll()
    if (campaignId) getSessionAudit(campaignId).then((p) => setSessions(p.sessions)).catch(() => {})
    const id = setInterval(() => void poll(), POLL_MS)
    return () => clearInterval(id)
  }, [poll, campaignId])

  const threads = useMemo(() => buildThreads(rows), [rows])
  const sessionById = useMemo(
    () => new Map(sessions.map((s) => [s.session_id, s])),
    [sessions],
  )

  const counts = useMemo(() => {
    const c: Record<string, number> = {}
    for (const [key, def] of Object.entries(FAILURES)) {
      c[key] = threads.filter((t) =>
        t.rows.some((r) => (def.kinds as readonly string[]).includes(r.kind)),
      ).length
    }
    return c
  }, [threads])

  const visible =
    filter === 'all'
      ? threads
      : threads.filter((t) =>
          t.rows.some((r) =>
            (FAILURES[filter].kinds as readonly string[]).includes(r.kind),
          ),
        )

  const spent = campaign ? (campaign.spent_paise / campaign.budget_paise) * 100 : 0
  const held = campaign
    ? Math.min(100 - spent, (campaign.reserved_paise / campaign.budget_paise) * 100)
    : 0

  return (
    <MerchantShell
      title={campaign?.name ?? 'Campaign'}
      subtitle={
        campaign
          ? `${campaign.status} · cap ${campaign.max_discount_bps / 100}% · floor ${campaign.margin_floor_bps / 100}%`
          : undefined
      }
      actions={
        campaignId && (
          <a
            href={qrSheetUrl(campaignId)}
            target="_blank"
            rel="noreferrer"
            className="rounded-[10px] bg-ink px-4 py-2.5 text-half font-semibold text-white transition-colors hover:bg-sidebar-hover"
          >
            Print stickers
          </a>
        )
      }
    >
      {justCommitted && campaign?.merkle_root && (
        <Card className="mb-5 border-pass-line bg-pass-bg">
          <p className="font-display text-lg font-medium text-pass">
            Committed · {campaign.slots_total} stickers generated
          </p>
          <p className="mt-1.5 break-all font-mono text-2xs text-ink-soft">
            root {campaign.merkle_root}
          </p>
          <p className="mt-1.5 text-mini text-ink-soft">
            Print the sheet now — these ceilings can no longer change.
          </p>
        </Card>
      )}

      {error && (
        <Card className="mb-5 border-fail-line bg-fail-bg text-mini text-fail">
          {error}
        </Card>
      )}

      {campaign && (
        <div className="mb-6 grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,420px)]">
          <Card>
            <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2 text-mini">
              <span className="text-ink-soft">
                <Money paise={campaign.spent_paise} /> given away
                {campaign.reserved_paise > 0 && (
                  <>
                    {' · '}
                    <Money paise={campaign.reserved_paise} /> held in checkout
                  </>
                )}
              </span>
              <span className="font-medium">
                <Money paise={campaign.remaining_paise} /> left of{' '}
                <Money paise={campaign.budget_paise} />
              </span>
            </div>
            <div className="flex h-3 overflow-hidden rounded-full bg-sunk">
              <div className="bg-accent-strong" style={{ width: `${spent}%` }} />
              <div className="bg-accent" style={{ width: `${held}%` }} />
            </div>
            <p className="mt-2 text-xxs text-ink-soft">
              {campaign.slots_redeemed} of {campaign.slots_total} stickers redeemed ·{' '}
              {campaign.slots_verified} verified at the counter
            </p>
          </Card>

          <div className="grid grid-cols-3 gap-3">
            <Stat label="Chats" value={threads.length} />
            <Stat label="Capped" value={counts.clamped ?? 0} tone="text-warn" />
            <Stat label="Blocked" value={counts.blocked ?? 0} tone="text-fail" />
          </div>
        </div>
      )}

      {/* --------------------------------------------- the failure gallery -- */}
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => setFilter('all')}
          className={`rounded-lg px-3 py-1.5 text-mini transition-colors ${
            filter === 'all'
              ? 'bg-ink text-white'
              : 'border border-hairline bg-card hover:bg-sunk'
          }`}
        >
          All conversations
        </button>
        {(Object.keys(FAILURES) as (keyof typeof FAILURES)[]).map((k) => (
          <button
            key={k}
            type="button"
            onClick={() => setFilter(k)}
            disabled={!counts[k]}
            className={`rounded-lg px-3 py-1.5 text-mini transition-colors disabled:opacity-35 ${
              filter === k
                ? 'bg-ink text-white'
                : 'border border-hairline bg-card hover:bg-sunk'
            }`}
          >
            {FAILURES[k].label}
            <span className="ml-1.5 tabular-nums opacity-70">{counts[k] ?? 0}</span>
          </button>
        ))}
        <label className="ml-auto flex items-center gap-2 text-xxs text-ink-soft">
          <input
            type="checkbox"
            checked={showTools}
            onChange={(e) => setShowTools(e.target.checked)}
          />
          show tool calls
        </label>
      </div>

      {filter !== 'all' && (
        <Card className="mb-4 border-accent bg-accent-soft">
          <p className="text-mini leading-relaxed">{FAILURES[filter].note}</p>
        </Card>
      )}

      {visible.length === 0 && (
        <Card className="text-center">
          <p className="text-mini text-ink-soft">
            {rows.length === 0
              ? 'Waiting for the first scan…'
              : 'Nothing matches this filter yet.'}
          </p>
        </Card>
      )}

      <div className="space-y-4">
        {visible.map((t) => {
          const shown = showTools
            ? t.rows
            : t.rows.filter((r) => r.kind !== 'tool_call')
          const session = sessionById.get(t.sessionId)
          const tone =
            t.outcome === 'settled'
              ? 'pass'
              : t.outcome === 'blocked' || t.outcome === 'refused'
                ? 'fail'
                : t.outcome === 'offered'
                  ? 'warn'
                  : 'neutral'

          return (
            <Card key={t.sessionId} padded={false}>
              <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-hairline px-4 py-3 md:px-5">
                <Pill tone={tone} dot>
                  {t.outcome}
                </Pill>
                {session && (
                  <span className="font-mono text-2xs text-ink-soft">
                    {session.slot_token}
                  </span>
                )}
                <span className="font-mono text-2xs tabular-nums text-ink-soft">
                  {clockOf(t.started)}
                </span>
                {t.widestAsk && t.widestAsk.granted < t.widestAsk.proposed && (
                  <span className="text-xxs text-ink-soft">
                    biggest ask <Pct bps={t.widestAsk.proposed} /> →{' '}
                    <Pct bps={t.widestAsk.granted} className="text-pass" />
                  </span>
                )}
                {session && (
                  <button
                    type="button"
                    onClick={() =>
                      setOpenScope(openScope === t.sessionId ? null : t.sessionId)
                    }
                    className="ml-auto text-2xs text-ink-soft underline underline-offset-2 hover:text-ink"
                  >
                    {openScope === t.sessionId ? 'hide scope' : 'what it could see'}
                  </button>
                )}
              </div>

              {session && openScope === t.sessionId && (
                <div className="px-4 pb-1 md:px-5">
                  <ScopePanel s={session} />
                </div>
              )}

              <ol className="py-1">
                {shown.map((r) => (
                  <LogRow key={r.id} r={r} />
                ))}
              </ol>
            </Card>
          )
        })}
      </div>
    </MerchantShell>
  )
}
