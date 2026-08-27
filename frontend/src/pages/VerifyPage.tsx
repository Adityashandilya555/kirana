import { useCallback, useState } from 'react'
import ProofPanel from '../components/ProofPanel'
import type { Walk } from '../components/ProofPanel'
import QrScanner from '../components/QrScanner'
import { ApiError, apiPost, rupees } from '../lib/api'

/**
 * The merchant's counter screen. The payoff of the whole pitch.
 *
 * Green on first scan, red on every scan after. The burn-once decision is made
 * in plpgsql under a row lock, not here — two staff scanning the same screen
 * at once is a race, and the database is the only place that can settle it.
 * This page renders the verdict and, underneath, the walk that shows the
 * merchant kept a promise made before anyone scanned anything.
 */

interface VerifyResult {
  valid: boolean
  code: string
  headline: string
  detail: string
  first_verified_at?: string | null
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

  const check = useCallback(async (raw: string) => {
    setBusy(true)
    setError(null)
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
                {result.sku && (
                  <div className="flex justify-between gap-4">
                    <dt className="text-ink-soft">item</dt>
                    <dd>
                      {result.sku} × {result.qty ?? 1}
                    </dd>
                  </div>
                )}
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
