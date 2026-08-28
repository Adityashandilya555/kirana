import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import MerchantShell from '../../components/MerchantShell'
import { Button, Card, Field, Money, inputClass } from '../../components/ui'
import {
  commitCampaign,
  createCampaign,
  getCatalog,
  getShelves,
  simulate,
} from '../../lib/merchant'
import type { CatalogItem, Shelf, Simulation } from '../../lib/merchant'

/**
 * Plan a campaign, see what it will actually do, then freeze it.
 *
 * The simulator is the reason this screen exists rather than a plain form.
 * Commit is irreversible and the sheet gets printed straight afterwards, so
 * the only moment to discover that a 12% margin floor makes half the shop
 * undiscountable is *before* pressing it. The preview runs on the server
 * against the same pure functions the live gate uses, so it cannot drift from
 * what will really happen.
 */

export default function NewCampaignPage() {
  const navigate = useNavigate()

  const [name, setName] = useState('Diwali Haggle')
  const [budget, setBudget] = useState('5000')
  const [maxDiscount, setMaxDiscount] = useState('20')
  const [marginFloor, setMarginFloor] = useState('12')
  const [maxTurns, setMaxTurns] = useState('6')
  const [slotCount, setSlotCount] = useState('24')
  const [binding, setBinding] = useState<'open' | 'product' | 'shelf'>('open')
  const [targets, setTargets] = useState<Set<string>>(new Set())

  const [catalog, setCatalog] = useState<CatalogItem[]>([])
  const [shelves, setShelves] = useState<Shelf[]>([])
  const [sim, setSim] = useState<Simulation | null>(null)
  const [simError, setSimError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    Promise.all([getCatalog(), getShelves()])
      .then(([c, s]) => {
        setCatalog(c.items)
        setShelves(s.shelves)
      })
      .catch(() => {})
  }, [])

  const budgetPaise = Math.round(parseFloat(budget || '0') * 100)
  const maxBps = Math.round(parseFloat(maxDiscount || '0') * 100)
  const floorBps = Math.round(parseFloat(marginFloor || '0') * 100)
  const slots = parseInt(slotCount || '0', 10)

  const run = useCallback(async () => {
    if (!budgetPaise || !slots || catalog.length === 0) return
    try {
      setSim(await simulate({
        max_discount_bps: maxBps,
        margin_floor_bps: floorBps,
        budget_paise: budgetPaise,
        slot_count: slots,
      }))
      setSimError(null)
    } catch (e) {
      setSim(null)
      setSimError(e instanceof Error ? e.message : String(e))
    }
  }, [budgetPaise, maxBps, floorBps, slots, catalog.length])

  // Debounced: these are number inputs and a request per keystroke would both
  // hammer the API and make the panel flicker while a value is half-typed.
  useEffect(() => {
    const t = setTimeout(() => void run(), 350)
    return () => clearTimeout(t)
  }, [run])

  const targetList = [...targets]
  const needsTargets = binding !== 'open'
  const canSubmit =
    !!name.trim() && budgetPaise > 0 && slots > 0 &&
    (!needsTargets || targetList.length > 0) && !busy

  async function createAndCommit() {
    setBusy(true)
    setError(null)
    try {
      const campaign = await createCampaign({
        name: name.trim(),
        budget_paise: budgetPaise,
        max_discount_bps: maxBps,
        margin_floor_bps: floorBps,
        max_turns: parseInt(maxTurns, 10),
        slot_count: slots,
        slot_binding: binding,
      })
      const committed = await commitCampaign(campaign.id, targetList)
      navigate(`/merchant/${committed.id}?committed=1`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <MerchantShell
      title="New campaign"
      subtitle="Set the limits, see what they do, then freeze them."
    >
      <div className="grid gap-6 lg:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
        {/* ----------------------------------------------------- form -- */}
        <div className="space-y-4">
          <Card className="space-y-4">
            <Field label="Campaign name">
              <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
            </Field>

            <div className="grid grid-cols-2 gap-3">
              <Field label="Total budget (₹)" hint="The most you will give away in discounts.">
                <input className={inputClass} inputMode="decimal" value={budget}
                       onChange={(e) => setBudget(e.target.value)} />
              </Field>
              <Field label="Stickers" hint="How many QR codes to print.">
                <input className={inputClass} inputMode="numeric" value={slotCount}
                       onChange={(e) => setSlotCount(e.target.value)} />
              </Field>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <Field label="Maximum discount (%)" hint="No sticker may ever exceed this.">
                <input className={inputClass} inputMode="decimal" value={maxDiscount}
                       onChange={(e) => setMaxDiscount(e.target.value)} />
              </Field>
              <Field label="Margin floor (%)" hint="Profit you refuse to go below.">
                <input className={inputClass} inputMode="decimal" value={marginFloor}
                       onChange={(e) => setMarginFloor(e.target.value)} />
              </Field>
            </div>

            <Field label="Conversation limit" hint="Rounds of haggling before the price is final.">
              <input className={inputClass} inputMode="numeric" value={maxTurns}
                     onChange={(e) => setMaxTurns(e.target.value)} />
            </Field>
          </Card>

          {/* ------------------------------------------------- binding -- */}
          <Card>
            <p className="mb-1 text-sm font-medium">What does each sticker cover?</p>
            <p className="mb-3 text-xs text-ink-soft">
              A bound sticker narrows the assistant's world: it is not told to avoid
              other products, it simply never learns they exist.
            </p>
            <div className="grid gap-2">
              {([
                ['open', 'Anything in the shop', 'One sticker works for every product.'],
                ['product', 'One product each', 'Stickers are shared out across the products you pick.'],
                ['shelf', 'A shelf each', 'Stickers are shared out across the shelves you pick.'],
              ] as const).map(([value, label, hint]) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => {
                    setBinding(value)
                    setTargets(new Set())
                  }}
                  className={`rounded-lg border px-3 py-2 text-left text-sm transition-colors ${
                    binding === value
                      ? 'border-accent bg-accent-soft'
                      : 'border-hairline hover:bg-sunk'
                  }`}
                >
                  <span className="block font-medium">{label}</span>
                  <span className="text-xs text-ink-soft">{hint}</span>
                </button>
              ))}
            </div>

            {binding === 'product' && (
              <div className="mt-3 grid gap-2 sm:grid-cols-2">
                {catalog.map((c) => (
                  <button
                    key={c.sku} type="button"
                    onClick={() => setTargets((p) => {
                      const n = new Set(p)
                      n.has(c.sku) ? n.delete(c.sku) : n.add(c.sku)
                      return n
                    })}
                    className={`rounded-lg border px-3 py-2 text-left text-sm ${
                      targets.has(c.sku) ? 'border-accent bg-accent-soft' : 'border-hairline hover:bg-sunk'
                    }`}
                  >
                    <span className="block truncate">{c.name}</span>
                    <span className="font-mono text-[11px] text-ink-soft">{c.sku}</span>
                  </button>
                ))}
              </div>
            )}

            {binding === 'shelf' && (
              <div className="mt-3 grid gap-2">
                {shelves.length === 0 && (
                  <p className="text-sm text-ink-soft">
                    No shelves yet — create one under Shelves first.
                  </p>
                )}
                {shelves.map((s) => (
                  <button
                    key={s.id} type="button"
                    onClick={() => setTargets((p) => {
                      const n = new Set(p)
                      n.has(s.id) ? n.delete(s.id) : n.add(s.id)
                      return n
                    })}
                    className={`rounded-lg border px-3 py-2 text-left text-sm ${
                      targets.has(s.id) ? 'border-accent bg-accent-soft' : 'border-hairline hover:bg-sunk'
                    }`}
                  >
                    <span className="block font-medium">{s.name}</span>
                    <span className="text-xs text-ink-soft">{s.skus.join(', ')}</span>
                  </button>
                ))}
              </div>
            )}

            {needsTargets && targetList.length > 0 && slots > 0 && (
              <p className="mt-3 text-xs text-ink-soft">
                {slots} stickers shared round-robin across {targetList.length} —
                about {Math.floor(slots / targetList.length)} each.
              </p>
            )}
          </Card>

          {error && (
            <Card className="border-fail-line bg-fail-bg text-sm text-fail">{error}</Card>
          )}

          <Card className="border-accent/30 bg-accent-soft">
            <p className="text-sm font-medium">Committing is permanent</p>
            <p className="mt-1 text-xs text-ink-soft">
              It generates every sticker and freezes a cryptographic root. After this the
              ceilings cannot be changed without printing a new sheet.
            </p>
            <div className="mt-3">
              <Button onClick={() => void createAndCommit()} disabled={!canSubmit}>
                {busy ? 'Committing…' : 'Commit and generate stickers'}
              </Button>
            </div>
          </Card>
        </div>

        {/* ------------------------------------------------ simulator -- */}
        <div className="space-y-4">
          {simError && (
            <Card className="border-warn-line bg-warn-bg text-sm text-warn">{simError}</Card>
          )}

          {sim?.warnings.map((w, i) => (
            <Card
              key={i}
              className={
                w.level === 'stop'
                  ? 'border-fail-line bg-fail-bg'
                  : w.level === 'warn'
                    ? 'border-warn-line bg-warn-bg'
                    : 'border-accent/30 bg-accent-soft'
              }
            >
              <p className="text-sm">{w.message}</p>
            </Card>
          ))}

          {sim && (
            <>
              <Card>
                <h2 className="mb-1 font-medium">What each product can actually do</h2>
                <p className="mb-3 text-xs text-ink-soft">
                  The margin floor usually bites before your maximum discount does.
                </p>
                <div className="overflow-x-auto rounded-lg border border-hairline">
                  <table className="w-full text-sm">
                    <thead className="bg-sunk text-left text-[11px] uppercase tracking-wide text-ink-soft">
                      <tr>
                        <th className="px-3 py-2">Product</th>
                        <th className="px-3 py-2 text-right">Margin</th>
                        <th className="px-3 py-2 text-right">Best offer</th>
                        <th className="px-3 py-2">Held by</th>
                      </tr>
                    </thead>
                    <tbody>
                      {sim.items.map((i) => (
                        <tr
                          key={i.sku}
                          className={`border-t border-hairline ${i.discountable ? '' : 'bg-fail-bg'}`}
                        >
                          <td className="px-3 py-2">
                            <span className="block truncate">{i.name}</span>
                            <span className="font-mono text-[11px] text-ink-soft">{i.sku}</span>
                          </td>
                          <td className="tnum px-3 py-2 text-right">{i.margin_at_list_pct}%</td>
                          <td className="tnum px-3 py-2 text-right font-medium">
                            {i.discountable ? `${i.max_discount_pct}%` : 'none'}
                          </td>
                          <td className="px-3 py-2 text-xs text-ink-soft">
                            {i.binding === 'margin_floor_bps' ? 'margin floor' : 'campaign cap'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>

              <div className="grid gap-4 sm:grid-cols-2">
                <Card>
                  <h3 className="mb-2 text-sm font-medium">Sticker spread</h3>
                  <ul className="space-y-1.5 text-sm">
                    {sim.ceiling_tiers.map((t) => (
                      <li key={t.ceiling_bps} className="flex items-center gap-2">
                        <span className="tnum w-14 text-right font-medium">{t.ceiling_pct}%</span>
                        <span
                          className="h-2 rounded-full bg-accent"
                          style={{ width: `${(t.slots / Math.max(1, slots)) * 120}px` }}
                        />
                        <span className="tnum text-xs text-ink-soft">{t.slots}</span>
                      </li>
                    ))}
                  </ul>
                </Card>

                <Card>
                  <h3 className="mb-2 text-sm font-medium">Budget reach</h3>
                  <p className="text-sm">
                    Worst case <Money paise={sim.budget.worst_case_total_paise} /> of{' '}
                    <Money paise={sim.budget.budget_paise} />
                  </p>
                  <p className="mt-1 text-xs text-ink-soft">
                    {sim.budget.covers_all_slots
                      ? 'Your budget covers every sticker even if all are redeemed at their ceiling.'
                      : `The budget rule would start refusing after about ${sim.budget.slots_before_exhausted} of ${sim.budget.slot_count} stickers.`}
                  </p>
                </Card>
              </div>
            </>
          )}

          {!sim && !simError && (
            <Card>
              <p className="text-sm text-ink-soft">
                {catalog.length === 0
                  ? 'Add products first — the preview needs something to price.'
                  : 'Adjust the numbers to see what they will do.'}
              </p>
            </Card>
          )}
        </div>
      </div>
    </MerchantShell>
  )
}
