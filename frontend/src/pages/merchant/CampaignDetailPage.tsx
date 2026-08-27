import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import MerchantShell, { Card, Money, Pct } from '../../components/MerchantShell'
import { getAudit, qrSheetUrl } from '../../lib/merchant'
import type { AuditRow, Campaign } from '../../lib/merchant'

/**
 * The live log, grouped into conversations.
 *
 * A flat feed of rows is what the database holds, and it is the wrong shape
 * for a person: the interesting unit is one shopper's negotiation, not one
 * decision. A judge watching this wants to follow a single haggle from scan to
 * settlement and see the gate intervene inside it — which a chronological list
 * of twelve interleaved rows actively hides.
 *
 * So rows are grouped by session and rendered as threads, newest first, with
 * the moment that matters — the model asking for more than it may have — given
 * its own visual treatment rather than being one line among many.
 *
 * Polling, not SSE: Railway closes idle SSE connections at five minutes and a
 * feed that dies silently mid-demo is worse than one costing a request every
 * 1.5 seconds.
 */

const POLL_MS = 1500

type Filter = 'all' | 'offers' | 'refusals' | 'blocked'

interface Thread {
  sessionId: string
  rows: AuditRow[]
  started: string
  slotToken: string | null
  outcome: 'blocked' | 'settled' | 'refused' | 'offered' | 'open'
  bestProposed: number | null
  bestGranted: number | null
}

const KIND_TONE: Record<string, string> = {
  approved: 'text-pass bg-pass-soft',
  clamped: 'text-warn bg-warn-soft',
  rejected: 'text-fail bg-fail-soft',
  injection_blocked: 'text-fail bg-fail-soft',
  verify_rejected: 'text-fail bg-fail-soft',
  llm_error: 'text-fail bg-fail-soft',
  llm_fallback: 'text-warn bg-warn-soft',
  settled: 'text-pass bg-pass-soft',
  verified: 'text-pass bg-pass-soft',
}

const KIND_LABEL: Record<string, string> = {
  session_opened: 'scanned',
  injection_blocked: 'attack blocked',
  tool_call: 'tool',
  approved: 'approved',
  clamped: 'capped',
  rejected: 'refused',
  llm_fallback: 'fallback',
  llm_error: 'model error',
  order_created: 'checkout',
  settled: 'paid',
  payment_failed: 'payment failed',
  verified: 'verified',
  verify_rejected: 'reuse refused',
  campaign_committed: 'committed',
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
        : kinds.has('rejected') && !kinds.has('approved') && !kinds.has('clamped')
          ? 'refused'
          : kinds.has('approved') || kinds.has('clamped')
            ? 'offered'
            : 'open'

    // The headline number for the thread is the widest gap the gate closed.
    let bestProposed: number | null = null
    let bestGranted: number | null = null
    let widest = -1
    for (const r of ordered) {
      if (r.proposed_bps != null && r.granted_bps != null) {
        const gap = r.proposed_bps - r.granted_bps
        if (gap > widest) {
          widest = gap
          bestProposed = r.proposed_bps
          bestGranted = r.granted_bps
        }
      }
    }
    threads.push({
      sessionId,
      rows: ordered,
      started: ordered[0]?.created_at ?? '',
      slotToken: null,
      outcome,
      bestProposed,
      bestGranted,
    })
  }
  return threads.sort((a, b) => (a.started < b.started ? 1 : -1))
}

function ClampBar({ proposed, granted }: { proposed: number; granted: number }) {
  const width = Math.max(4, Math.min(100, (granted / Math.max(proposed, 1)) * 100))
  return (
    <div className="mt-2">
      <div className="flex items-baseline justify-between text-xs">
        <span className="text-ink-soft">
          agent asked <span className="tnum font-medium text-ink"><Pct bps={proposed} /></span>
        </span>
        <span className="text-ink-soft">
          granted <span className="tnum font-medium text-pass"><Pct bps={granted} /></span>
        </span>
      </div>
      <div className="mt-1 h-2.5 overflow-hidden rounded-full bg-fail-soft">
        <div className="h-full rounded-full bg-pass" style={{ width: `${width}%` }} />
      </div>
    </div>
  )
}

function Row({ r }: { r: AuditRow }) {
  const tone = KIND_TONE[r.kind] ?? 'text-ink-soft bg-sunk'
  const clamped = r.proposed_bps != null && r.granted_bps != null && r.granted_bps < r.proposed_bps
  return (
    <li className="border-t border-hairline py-2.5 first:border-t-0">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className={`chip ${tone}`}>{KIND_LABEL[r.kind] ?? r.kind}</span>
        <span className="font-mono text-[11px] text-ink-soft">{r.code}</span>
        {r.binding_constraint && (
          <span className="text-[11px] text-ink-soft">
            held by <span className="font-mono">{r.binding_constraint}</span>
          </span>
        )}
        <span className="ml-auto flex items-center gap-2 text-[11px] text-ink-soft">
          {/* A null provider on a blocked row is the machine-checkable proof
              that no model was consulted. Render it, never hide it. */}
          {r.llm_provider ? (
            <span className="font-mono">{r.llm_provider}</span>
          ) : r.kind === 'injection_blocked' ? (
            <span className="font-mono text-fail">no model called</span>
          ) : null}
          {r.latency_ms ? <span className="tnum">{r.latency_ms}ms</span> : null}
        </span>
      </div>

      {clamped && <ClampBar proposed={r.proposed_bps!} granted={r.granted_bps!} />}

      {r.raw_user_message && (
        <p className="mt-1.5 border-l-2 border-hairline pl-2 text-xs italic text-ink-soft">
          “{r.raw_user_message}”
        </p>
      )}
      <p className="mt-1 text-sm leading-relaxed">{r.human_reason}</p>
    </li>
  )
}

export default function CampaignDetailPage() {
  const { campaignId } = useParams<{ campaignId: string }>()
  const [params] = useSearchParams()
  const justCommitted = params.get('committed') === '1'

  const [campaign, setCampaign] = useState<Campaign | null>(null)
  const [rows, setRows] = useState<AuditRow[]>([])
  const [filter, setFilter] = useState<Filter>('all')
  const [error, setError] = useState<string | null>(null)
  const [showTools, setShowTools] = useState(false)
  const cursor = useRef(0)

  const poll = useCallback(async () => {
    if (!campaignId) return
    try {
      const feed = await getAudit(campaignId, cursor.current)
      setCampaign(feed.campaign)
      if (feed.items?.length) {
        cursor.current = feed.cursor
        setRows((r) => [...r, ...feed.items].slice(-800))
      }
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [campaignId])

  useEffect(() => {
    void poll()
    const id = setInterval(() => void poll(), POLL_MS)
    return () => clearInterval(id)
  }, [poll])

  const threads = useMemo(() => buildThreads(rows), [rows])

  const stats = useMemo(() => {
    const s = { conversations: threads.length, blocked: 0, capped: 0, settled: 0, refused: 0 }
    for (const t of threads) {
      if (t.outcome === 'blocked') s.blocked += 1
      if (t.outcome === 'settled') s.settled += 1
      if (t.outcome === 'refused') s.refused += 1
      if (t.rows.some((r) => r.kind === 'clamped')) s.capped += 1
    }
    return s
  }, [threads])

  const visible = threads.filter((t) => {
    if (filter === 'all') return true
    if (filter === 'blocked') return t.outcome === 'blocked'
    if (filter === 'refusals')
      return t.outcome === 'refused' || t.rows.some((r) => r.kind === 'clamped')
    return t.outcome === 'offered' || t.outcome === 'settled'
  })

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
            className="rounded-lg bg-accent px-3.5 py-2 text-sm font-medium text-white hover:opacity-90"
          >
            Print stickers
          </a>
        )
      }
    >
      {justCommitted && campaign?.merkle_root && (
        <Card className="mb-4 border-pass/40 bg-pass-soft">
          <p className="font-medium text-pass">Committed. {campaign.slots_total} stickers generated.</p>
          <p className="mt-1 break-all font-mono text-[11px] text-ink-soft">
            root {campaign.merkle_root}
          </p>
          <p className="mt-1 text-xs text-ink-soft">
            Print the sheet now — these ceilings can no longer change.
          </p>
        </Card>
      )}

      {error && (
        <Card className="mb-4 border-fail/40 bg-fail-soft text-sm text-fail">{error}</Card>
      )}

      {/* ------------------------------------------------- budget + stats -- */}
      {campaign && (
        <div className="mb-6 grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,380px)]">
          <Card>
            <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2 text-sm">
              <span className="text-ink-soft">
                <Money paise={campaign.spent_paise} /> given away
                {campaign.reserved_paise > 0 && (
                  <> · <Money paise={campaign.reserved_paise} /> held in checkout</>
                )}
              </span>
              <span className="font-medium">
                <Money paise={campaign.remaining_paise} /> left of{' '}
                <Money paise={campaign.budget_paise} />
              </span>
            </div>
            <div className="flex h-3 overflow-hidden rounded-full bg-sunk">
              <div className="bg-accent" style={{ width: `${spent}%` }} />
              <div className="bg-accent/40" style={{ width: `${held}%` }} />
            </div>
            <p className="mt-2 text-xs text-ink-soft">
              {campaign.slots_redeemed} of {campaign.slots_total} stickers redeemed ·{' '}
              {campaign.slots_verified} verified at the counter
            </p>
          </Card>

          <Card>
            <dl className="grid grid-cols-4 gap-2 text-center">
              {([
                ['Chats', stats.conversations, ''],
                ['Capped', stats.capped, 'text-warn'],
                ['Blocked', stats.blocked, 'text-fail'],
                ['Paid', stats.settled, 'text-pass'],
              ] as const).map(([label, value, tone]) => (
                <div key={label}>
                  <dt className="text-[11px] uppercase tracking-wide text-ink-soft">{label}</dt>
                  <dd className={`tnum text-xl font-semibold ${tone}`}>{value}</dd>
                </div>
              ))}
            </dl>
          </Card>
        </div>
      )}

      {/* ------------------------------------------------------- filters -- */}
      <div className="mb-4 flex flex-wrap items-center gap-2">
        {(['all', 'offers', 'refusals', 'blocked'] as Filter[]).map((f) => (
          <button
            key={f}
            type="button"
            onClick={() => setFilter(f)}
            className={`rounded-lg px-3 py-1.5 text-sm capitalize transition-colors ${
              filter === f ? 'bg-accent text-white' : 'border border-hairline bg-surface hover:bg-sunk'
            }`}
          >
            {f}
          </button>
        ))}
        <label className="ml-auto flex items-center gap-2 text-xs text-ink-soft">
          <input type="checkbox" checked={showTools} onChange={(e) => setShowTools(e.target.checked)} />
          show tool calls
        </label>
      </div>

      {/* ------------------------------------------------------ threads -- */}
      {visible.length === 0 && (
        <Card className="text-center">
          <p className="text-sm text-ink-soft">
            {rows.length === 0
              ? 'Waiting for the first scan…'
              : 'No conversations match this filter.'}
          </p>
        </Card>
      )}

      <div className="space-y-4">
        {visible.map((t) => {
          const shown = showTools ? t.rows : t.rows.filter((r) => r.kind !== 'tool_call')
          const badge = {
            blocked: 'text-fail bg-fail-soft',
            settled: 'text-pass bg-pass-soft',
            refused: 'text-fail bg-fail-soft',
            offered: 'text-warn bg-warn-soft',
            open: 'text-ink-soft bg-sunk',
          }[t.outcome]
          return (
            <Card key={t.sessionId}>
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <span className={`chip ${badge}`}>{t.outcome}</span>
                <span className="text-xs text-ink-soft">
                  {new Date(t.started).toLocaleTimeString()}
                </span>
                {t.bestProposed != null && t.bestGranted != null &&
                  t.bestGranted < t.bestProposed && (
                    <span className="text-xs text-ink-soft">
                      biggest ask <Pct bps={t.bestProposed} /> → <Pct bps={t.bestGranted} />
                    </span>
                  )}
                <span className="ml-auto font-mono text-[11px] text-ink-soft">
                  {t.sessionId.slice(0, 8)}
                </span>
              </div>
              <ul>
                {shown.map((r) => (
                  <Row key={r.id} r={r} />
                ))}
              </ul>
            </Card>
          )
        })}
      </div>
    </MerchantShell>
  )
}
