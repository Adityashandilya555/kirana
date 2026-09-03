import { useCallback, useState } from 'react'
import ProofPanel from '../components/ProofPanel'
import type { Walk } from '../components/ProofPanel'
import QrScanner from '../components/QrScanner'
import { ApiError, apiPost, pct, rupees } from '../lib/api'

/**
 * The merchant's counter screen. The payoff of the whole pitch.
 *
 * Green on first scan, red on every scan after. The burn-once decision is made
 * in plpgsql under a row lock, not here — two staff scanning the same screen
 * at once is a race, and the database is the only place that can settle it.
 * This page renders the verdict and, underneath, the walk that shows the
 * merchant kept a promise made before anyone scanned anything.
 *
 * WHAT IT WAS MISSING. A shopkeeper who scanned a code learned the sku, the
 * quantity and a cryptographic argument, and nothing whatsoever about the
 * person standing in front of them — not that this was their eleventh visit,
 * not what they had spent, not even the last four digits of the number they
 * gave at the door. The whole reason the door asks for a number is that it
 * comes back out at the counter. It does now, above the proof, because at a
 * counter the person is the first question and the proof is the second.
 *
 * And a bill. The basket is what the customer actually bought, so it is what
 * has to be handed over — behind a button rather than always open, because the
 * common case is a shopkeeper glancing at a green tick and waving somebody
 * through.
 */

interface BillLine {
  sku: string
  name: string
  unit: string
  qty: number
  unit_price_paise: number
  gross_paise: number
  granted_bps: number
  discount_paise: number
  line_total_paise: number
}

interface VerifyResult {
  valid: boolean
  code: string
  headline: string
  detail: string
  first_verified_at?: string | null
  paid_at?: string | null
  store?: string
  campaign_name?: string
  sku?: string | null
  qty?: number | null
  slot_token?: string
  leaf_index?: number
  granted_bps?: number | null
  ceiling_bps?: number | null
  discount_paise?: number | null
  final_amount_paise?: number | null
  within_ceiling?: boolean
  argument?: string
  /** Who is at the counter. `phone_last4` only — the number itself is never
   *  sent back out, which is the same rule the session-opened audit row
   *  follows. `visits` and `spend_paise` EXCLUDE the purchase being verified,
   *  so "8 visits" means eight before this one. */
  customer?: {
    identified: boolean
    phone_last4?: string | null
    display_name?: string | null
    visits?: number
    spend_paise?: number
    saved_paise?: number
    last_visit_at?: string | null
    first_seen_at?: string | null
    returning?: boolean
    band?: string | null
  }
  bill?: {
    items: BillLine[]
    count: number
    gross_paise: number
    discount_paise: number
    total_paise: number
  }
  commitment?: {
    merkle_root: string
    policy_hash: string
    tree_size: number
    committed_at: string
    leaf_hash: string
  }
  proof: Walk | null
}

export default function VerifyPage() {
  const [result, setResult] = useState<VerifyResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [bill, setBill] = useState(false)

  const check = useCallback(async (raw: string) => {
    setBusy(true)
    setError(null)
    setBill(false)
    try {
      setResult(await apiPost<VerifyResult>('/api/v1/verify', { token: raw }, true))
    } catch (e) {
      setResult(null)
      setError(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e))
    } finally {
      setBusy(false)
    }
  }, [])

  const green = result?.valid === true
  const customer = result?.customer
  const lines = result?.bill?.items ?? []

  return (
    <main className="mx-auto max-w-md space-y-4 p-4">
      <h1 className="text-lg font-semibold text-ink">Verify a redemption</h1>

      <QrScanner onToken={(raw) => void check(raw)} busy={busy} />

      {error && (
        <p className="rounded-xl border border-fail/30 bg-fail/5 p-3 text-sm text-fail">
          {error}
        </p>
      )}

      {result && (
        <>
          <section
            className={`rounded-2xl border-2 p-4 ${
              green
                ? 'border-pass bg-pass/5'
                : 'border-fail bg-fail/5'
            }`}
          >
            <p
              className={`text-2xl font-semibold ${green ? 'text-pass' : 'text-fail'}`}
            >
              {green ? '✓ ' : '✗ '}
              {result.headline}
            </p>
            <p className="mt-1 text-sm leading-relaxed text-ink">{result.detail}</p>

            {result.slot_token && (
              <dl className="mt-3 space-y-1 border-t border-current/15 pt-3 text-sm">
                <div className="flex justify-between gap-4">
                  <dt className="text-ink-soft">code</dt>
                  <dd className="font-mono">{result.slot_token}</dd>
                </div>
                <div className="flex justify-between gap-4">
                  <dt className="text-ink-soft">items</dt>
                  <dd>
                    {lines.length > 0
                      ? `${result.bill?.count} line${result.bill?.count === 1 ? '' : 's'}`
                      : `${result.sku ?? '—'} × ${result.qty ?? 1}`}
                  </dd>
                </div>
                {result.final_amount_paise != null && (
                  <div className="flex justify-between gap-4">
                    <dt className="text-ink-soft">paid</dt>
                    <dd className="font-medium">
                      {rupees(result.final_amount_paise)}
                    </dd>
                  </div>
                )}
              </dl>
            )}

            {/* The sentence the demo says out loud. */}
            {result.argument && (
              <p
                className={`mt-3 rounded-lg px-3 py-2 font-mono text-sm ${
                  result.within_ceiling
                    ? 'bg-pass/10 text-pass'
                    : 'bg-fail/10 text-fail'
                }`}
              >
                {result.argument}
              </p>
            )}
          </section>

          {/* Who is at the counter. Above the proof on purpose: a shopkeeper
              serving someone looks at the person first. */}
          {customer && (
            <section className="rounded-2xl border border-hairline bg-card p-4 shadow-card">
              <p className="text-2xs font-semibold uppercase tracking-[0.14em] text-ink-soft">
                customer
              </p>
              {customer.identified ? (
                <>
                  <div className="mt-2 flex items-baseline justify-between gap-3">
                    <p className="font-display text-[17px] font-semibold text-ink">
                      {customer.display_name?.trim()
                        ? customer.display_name
                        : `…${customer.phone_last4 ?? '????'}`}
                    </p>
                    <span
                      className={`chip ${
                        customer.band === 'preferred'
                          ? 'border-pass-line bg-pass-bg text-pass'
                          : 'border-hairline bg-sunk text-ink-2'
                      }`}
                    >
                      {customer.band === 'preferred' ? 'regular' : 'new here'}
                    </span>
                  </div>

                  {/* Three numbers, and all three exclude today's purchase --
                      so "4 visits" means four before the one being scanned,
                      not a counter that ticks over as you look at it. */}
                  <dl className="mt-3 grid grid-cols-3 gap-3 border-t border-hairline pt-3">
                    <div>
                      <dt className="text-2xs text-ink-soft">past visits</dt>
                      <dd className="tnum mt-0.5 font-mono text-[17px] font-semibold text-ink">
                        {customer.visits ?? 0}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-2xs text-ink-soft">spent here</dt>
                      <dd className="tnum mt-0.5 font-mono text-[17px] font-semibold text-ink">
                        {rupees(customer.spend_paise ?? 0)}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-2xs text-ink-soft">saved here</dt>
                      <dd className="tnum mt-0.5 font-mono text-[17px] font-semibold text-pass">
                        {rupees(customer.saved_paise ?? 0)}
                      </dd>
                    </div>
                  </dl>

                  <p className="mt-3 text-xxs leading-relaxed text-ink-soft">
                    {customer.returning
                      ? `Last visit ${
                          customer.last_visit_at
                            ? new Date(customer.last_visit_at).toLocaleDateString()
                            : 'unknown'
                        }. Known here since ${
                          customer.first_seen_at
                            ? new Date(customer.first_seen_at).toLocaleDateString()
                            : 'today'
                        }.`
                      : 'First purchase at this shop.'}{' '}
                    Priced as a{' '}
                    {customer.band === 'preferred' ? 'regular' : 'new shopper'} —
                    the band was fixed when they scanned, not now.
                  </p>
                </>
              ) : (
                <p className="mt-2 text-mini leading-relaxed text-ink-soft">
                  No number given at the door, so there is no history to show.
                  They were priced as a new shopper.
                </p>
              )}
            </section>
          )}

          {/* The bill. Behind a tap: the common case is a glance at a green
              tick, and an itemised table on every scan would bury the verdict
              that this screen exists to deliver. */}
          {(lines.length > 0 || result.final_amount_paise != null) && (
            <section className="rounded-2xl border border-hairline bg-card shadow-card">
              <button
                type="button"
                onClick={() => setBill((v) => !v)}
                aria-expanded={bill}
                className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left"
              >
                <span className="text-half font-semibold text-ink">
                  {bill ? 'Hide bill' : 'Bill'}
                </span>
                <span className="text-xxs text-ink-soft">
                  {rupees(result.final_amount_paise ?? 0)}
                  <span aria-hidden className="ml-1.5 text-2xs">
                    {bill ? '▲' : '▼'}
                  </span>
                </span>
              </button>

              {bill && (
                <div className="border-t border-hairline px-4 py-3">
                  <p className="text-xxs text-ink-soft">
                    {result.store}
                    {result.paid_at &&
                      ` · ${new Date(result.paid_at).toLocaleString()}`}
                  </p>

                  <table className="mt-3 w-full text-tiny">
                    <thead>
                      <tr className="border-b border-hairline text-2xs uppercase tracking-[0.1em] text-ink-soft">
                        <th className="py-1.5 text-left font-semibold">item</th>
                        <th className="py-1.5 text-right font-semibold">qty</th>
                        <th className="py-1.5 text-right font-semibold">off</th>
                        <th className="py-1.5 text-right font-semibold">amount</th>
                      </tr>
                    </thead>
                    <tbody>
                      {lines.map((line) => (
                        <tr key={line.sku} className="border-b border-hairline-faint">
                          <td className="py-2 pr-2">
                            <span className="text-ink">{line.name}</span>
                            <span className="ml-1 font-mono text-2xs text-ink-soft">
                              {line.sku}
                            </span>
                          </td>
                          <td className="tnum py-2 text-right font-mono">{line.qty}</td>
                          <td className="tnum py-2 text-right font-mono text-pass">
                            {line.granted_bps > 0 ? pct(line.granted_bps) : '—'}
                          </td>
                          <td className="tnum py-2 text-right font-mono">
                            {rupees(line.line_total_paise)}
                          </td>
                        </tr>
                      ))}
                      {lines.length === 0 && (
                        <tr>
                          <td colSpan={4} className="py-2 text-ink-soft">
                            {result.sku ?? 'item'} × {result.qty ?? 1}
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>

                  <dl className="mt-3 space-y-1 border-t border-hairline pt-3 text-tiny">
                    <div className="flex justify-between gap-4">
                      <dt className="text-ink-soft">shelf total</dt>
                      <dd className="tnum font-mono">
                        {rupees(result.bill?.gross_paise ?? 0)}
                      </dd>
                    </div>
                    <div className="flex justify-between gap-4">
                      <dt className="text-ink-soft">discount</dt>
                      <dd className="tnum font-mono text-pass">
                        −{rupees(result.bill?.discount_paise ?? result.discount_paise ?? 0)}
                      </dd>
                    </div>
                    <div className="flex justify-between gap-4 border-t border-hairline pt-1.5">
                      <dt className="font-semibold text-ink">paid</dt>
                      <dd className="tnum font-mono font-semibold text-ink">
                        {rupees(result.final_amount_paise ?? 0)}
                      </dd>
                    </div>
                  </dl>

                  <p className="mt-3 text-2xs leading-relaxed text-ink-soft">
                    Code {result.slot_token} · verified{' '}
                    {result.first_verified_at
                      ? new Date(result.first_verified_at).toLocaleString()
                      : '—'}
                  </p>
                </div>
              )}
            </section>
          )}

          {result.proof && result.commitment && (
            <ProofPanel
              walk={result.proof}
              leafHash={result.commitment.leaf_hash}
              leafIndex={result.leaf_index ?? 0}
              root={result.commitment.merkle_root}
              treeSize={result.commitment.tree_size}
              committedAt={result.commitment.committed_at}
            />
          )}
        </>
      )}
    </main>
  )
}
