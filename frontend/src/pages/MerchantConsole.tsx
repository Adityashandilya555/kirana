import { useCallback, useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { apiGet, pct, rupees } from '../lib/api'

/**
 * The merchant's live view. This is what gets projected -- never the phone:
 * Android payment apps set FLAG_SECURE and mirror as a black rectangle at the
 * worst possible moment.
 *
 * Polling, not SSE. Railway closes idle SSE connections at five minutes and
 * quick tunnels do not support them at all, and a feed that dies silently
 * mid-demo is worse than one that costs a request every 1.5s.
 */

const POLL_MS = 1500

interface AuditRow {
  id: number
  kind: string
  code: string
  proposed_bps: number | null
  granted_bps: number | null
  binding_constraint: string | null
  human_reason: string
  llm_provider: string | null
  llm_model: string | null
  latency_ms: number | null
  created_at: string
}

interface Campaign {
  id: string
  name: string
  status: string
  budget_paise: number
  spent_paise: number
  reserved_paise: number
  remaining_paise: number
  max_discount_bps: number
  margin_floor_bps: number
  merkle_root: string | null
  slots_total: number
  slots_redeemed: number
}

interface Feed {
  cursor: number
  campaign: Campaign
  items: AuditRow[]
}

const KIND_STYLE: Record<string, string> = {
  approved: 'bg-pass/10 text-pass border-pass/30',
  clamped: 'bg-amber-50 text-amber-800 border-amber-300',
  rejected: 'bg-fail/10 text-fail border-fail/30',
  injection_blocked: 'bg-fail/10 text-fail border-fail/30',
  verify_rejected: 'bg-fail/10 text-fail border-fail/30',
  llm_fallback: 'bg-amber-50 text-amber-800 border-amber-300',
  llm_error: 'bg-fail/10 text-fail border-fail/30',
}

export default function MerchantConsole() {
  const { campaignId } = useParams<{ campaignId: string }>()
  const [campaign, setCampaign] = useState<Campaign | null>(null)
  const [rows, setRows] = useState<AuditRow[]>([])
  const [error, setError] = useState<string | null>(null)
  const cursor = useRef(0)
  const stopped = useRef(false)

  const poll = useCallback(async () => {
    if (!campaignId) return
    try {
      const feed = await apiGet<Feed>(
        `/api/v1/campaigns/${campaignId}/audit?after_id=${cursor.current}&limit=100`,
        true,
      )
      setCampaign(feed.campaign)
      if (feed.items?.length) {
        cursor.current = feed.cursor
        // Newest first on screen; the feed returns ascending.
        setRows((r) => [...feed.items].reverse().concat(r).slice(0, 300))
      }
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [campaignId])

  useEffect(() => {
    stopped.current = false
    void poll()
    const id = setInterval(() => {
      if (!stopped.current) void poll()
    }, POLL_MS)
    return () => {
      stopped.current = true
      clearInterval(id)
    }
  }, [poll])

  if (!campaignId) {
    return (
      <main className="p-8">
        <p className="text-sm text-ink-soft">
          Open <code className="font-mono">/merchant/&lt;campaign-id&gt;</code>.
        </p>
      </main>
    )
  }

  const spentPct = campaign
    ? Math.min(100, Math.round((campaign.spent_paise / campaign.budget_paise) * 100))
    : 0
  const reservedPct = campaign
    ? Math.min(100 - spentPct, Math.round((campaign.reserved_paise / campaign.budget_paise) * 100))
    : 0

  return (
    <main className="mx-auto max-w-5xl p-6">
      <header className="mb-5">
        <h1 className="text-xl font-semibold text-ink">
          {campaign?.name ?? 'Loading…'}{' '}
          <span className="text-sm font-normal text-ink-soft">
            {campaign?.status}
          </span>
        </h1>
        {campaign?.merkle_root && (
          <p className="mt-1 font-mono text-xs text-ink-soft">
            root {campaign.merkle_root.slice(0, 32)}…
          </p>
        )}
      </header>

      {campaign && (
        <section className="mb-6 rounded-2xl border border-hairline bg-white p-4">
          <div className="mb-2 flex items-baseline justify-between text-sm">
            <span className="text-ink-soft">
              spent {rupees(campaign.spent_paise)} · reserved{' '}
              {rupees(campaign.reserved_paise)}
            </span>
            <span className="font-medium text-ink">
              {rupees(campaign.remaining_paise)} left of{' '}
              {rupees(campaign.budget_paise)}
            </span>
          </div>
          <div className="flex h-2.5 overflow-hidden rounded-full bg-slate-100">
            <div className="bg-accent" style={{ width: `${spentPct}%` }} />
            <div className="bg-accent/40" style={{ width: `${reservedPct}%` }} />
          </div>
          <div className="mt-3 flex gap-4 text-xs text-ink-soft">
            <span>cap {pct(campaign.max_discount_bps)}</span>
            <span>floor {pct(campaign.margin_floor_bps)}</span>
            <span>
              slots {campaign.slots_redeemed}/{campaign.slots_total} redeemed
            </span>
          </div>
        </section>
      )}

      {error && (
        <p className="mb-4 rounded-lg border border-fail/30 bg-fail/5 p-3 text-sm text-fail">
          feed error: {error}
        </p>
      )}

      <ol className="space-y-2">
        {rows.map((r) => (
          <li
            key={r.id}
            className={`rounded-xl border p-3 ${
              KIND_STYLE[r.kind] ?? 'border-hairline bg-white text-ink'
            }`}
          >
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="rounded-full border border-current/30 px-2 py-0.5 font-medium">
                {r.kind}
              </span>
              <span className="font-mono opacity-70">{r.code}</span>
              {r.proposed_bps != null && (
                <span className="font-mono">
                  asked {pct(r.proposed_bps)}
                  {r.granted_bps != null && ` → got ${pct(r.granted_bps)}`}
                </span>
              )}
              {r.binding_constraint && (
                <span className="font-mono opacity-70">by {r.binding_constraint}</span>
              )}
              <span className="ml-auto font-mono opacity-60">
                {/* A null provider on an injection_blocked row is the proof
                    that no model was consulted. Render it, do not hide it. */}
                {r.llm_provider ?? 'no model'}
                {r.latency_ms ? ` · ${r.latency_ms}ms` : ''}
              </span>
            </div>
            <p className="mt-1.5 text-sm leading-relaxed">{r.human_reason}</p>
          </li>
        ))}
        {rows.length === 0 && !error && (
          <li className="rounded-xl border border-hairline bg-white p-6 text-center text-sm text-ink-soft">
            Waiting for the first scan…
          </li>
        )}
      </ol>
    </main>
  )
}
