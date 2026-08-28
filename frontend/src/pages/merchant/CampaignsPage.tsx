import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import MerchantShell from '../../components/MerchantShell'
import { Button, Card, EmptyState, Money, Pct } from '../../components/ui'
import { getCampaigns, qrSheetUrl } from '../../lib/merchant'
import type { Campaign } from '../../lib/merchant'

const BINDING_LABEL: Record<string, string> = {
  open: 'Any product',
  product: 'One product per sticker',
  shelf: 'By shelf',
}

function BudgetBar({ c }: { c: Campaign }) {
  const spent = Math.min(100, (c.spent_paise / c.budget_paise) * 100)
  const reserved = Math.min(100 - spent, (c.reserved_paise / c.budget_paise) * 100)
  return (
    <div>
      <div className="flex h-2 overflow-hidden rounded-full bg-sunk">
        <div className="bg-accent" style={{ width: `${spent}%` }} />
        <div className="bg-accent/40" style={{ width: `${reserved}%` }} />
      </div>
      <p className="mt-1.5 text-xs text-ink-soft">
        <Money paise={c.spent_paise} /> spent of <Money paise={c.budget_paise} />
        {c.reserved_paise > 0 && (
          <> · <Money paise={c.reserved_paise} /> held</>
        )}
      </p>
    </div>
  )
}

export default function CampaignsPage() {
  const [campaigns, setCampaigns] = useState<Campaign[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const navigate = useNavigate()

  useEffect(() => {
    getCampaigns()
      .then((c) => setCampaigns(Array.isArray(c) ? c : []))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [])

  return (
    <MerchantShell
      title="Campaigns"
      subtitle="Each campaign is one printed sheet of discount stickers."
      actions={<Button onClick={() => navigate('/merchant/new')}>New campaign</Button>}
    >
      {error && (
        <Card className="mb-4 border-fail-line bg-fail-bg text-sm text-fail">{error}</Card>
      )}

      {campaigns === null && <p className="text-sm text-ink-soft">Loading…</p>}

      {campaigns?.length === 0 && (
        <EmptyState
          title="No campaigns yet"
          body="A campaign sets a budget and a discount limit, then produces a sheet of QR stickers for your shelves. Add your products first if you have not already."
          action={<Button onClick={() => navigate('/merchant/new')}>Plan your first campaign</Button>}
        />
      )}

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {campaigns?.map((c) => {
          const live = c.status === 'live'
          return (
            <Card key={c.id} className="flex flex-col gap-4">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <Link
                    to={`/merchant/${c.id}`}
                    className="block truncate font-medium hover:text-accent"
                  >
                    {c.name}
                  </Link>
                  <p className="mt-0.5 text-xs text-ink-soft">
                    {BINDING_LABEL[c.slot_binding] ?? c.slot_binding} · cap{' '}
                    <Pct bps={c.max_discount_bps} /> · floor{' '}
                    <Pct bps={c.margin_floor_bps} />
                  </p>
                </div>
                <span
                  className={`chip ${
                    live ? 'text-pass bg-pass-bg' : 'text-ink-soft bg-sunk'
                  }`}
                >
                  {c.status}
                </span>
              </div>

              <BudgetBar c={c} />

              <dl className="grid grid-cols-3 gap-2 border-t border-hairline pt-3 text-center">
                <div>
                  <dt className="text-[11px] uppercase tracking-wide text-ink-soft">Stickers</dt>
                  <dd className="tnum text-sm font-medium">{c.slots_total || c.slot_count}</dd>
                </div>
                <div>
                  <dt className="text-[11px] uppercase tracking-wide text-ink-soft">Redeemed</dt>
                  <dd className="tnum text-sm font-medium">{c.slots_redeemed}</dd>
                </div>
                <div>
                  <dt className="text-[11px] uppercase tracking-wide text-ink-soft">Verified</dt>
                  <dd className="tnum text-sm font-medium">{c.slots_verified}</dd>
                </div>
              </dl>

              {c.scopes?.length > 0 && (
                <p className="text-xs text-ink-soft">
                  Bound to{' '}
                  {c.scopes
                    .map((s) => `${s.shelf_name ?? s.bound_sku} (${s.slots})`)
                    .join(' · ')}
                </p>
              )}

              <div className="mt-auto flex flex-wrap gap-2">
                <Button variant="ghost" onClick={() => navigate(`/merchant/${c.id}`)}>
                  Live log
                </Button>
                {live && (
                  <a
                    href={qrSheetUrl(c.id)}
                    target="_blank"
                    rel="noreferrer"
                    className="rounded-lg border border-hairline bg-surface px-3.5 py-2 text-sm font-medium hover:bg-sunk"
                  >
                    Print stickers
                  </a>
                )}
                {!live && (
                  <Button variant="ghost" onClick={() => navigate(`/merchant/${c.id}`)}>
                    Finish setup
                  </Button>
                )}
              </div>
            </Card>
          )
        })}
      </div>
    </MerchantShell>
  )
}
