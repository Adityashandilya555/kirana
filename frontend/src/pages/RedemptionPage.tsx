import { useCallback, useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { API_BASE, ApiError, apiGet, pct, rupees } from '../lib/api'
import { slotLeafHash, verifyProofWalk } from '../lib/merkle'

/**
 * The customer's screen after paying: the QR the merchant scans, and a badge
 * proving the discount was inside a bound committed before anyone scanned.
 *
 * The badge is the reason merkle.ts exists as a twin of merkle.py. It does NOT
 * trust the leaf hash the server sent — it RECOMPUTES it from the opened
 * commitment (campaign id, leaf index, slot token, ceiling, salt) and only
 * then walks the proof to the root. Verifying a server-supplied leaf against a
 * server-supplied root would prove nothing at all; recomputing the leaf is
 * what makes this an independent check running on the customer's own device.
 *
 * This page never burns the code. It reads through get_redemption, which is a
 * separate function from verify_redemption for exactly that reason — a phone
 * that reloads must not spend the discount.
 */

interface Redemption {
  slot_token: string
  leaf_index: number
  salt_hex: string
  ceiling_bps: number
  granted_bps: number | null
  discount_paise: number | null
  leaf_hash: string
  proof: { hash: string; position: 'left' | 'right' }[]
  redeemed_at: string | null
  verified_at: string | null
  store: string
  store_line: string
  campaign_id: string
  campaign_name: string
  merkle_root: string
  tree_size: number
  committed_at: string
  sku: string | null
  qty: number | null
  final_amount_paise: number | null
}

type Check =
  | { state: 'checking' }
  | { state: 'ok' }
  | { state: 'bad'; why: string }

export default function RedemptionPage() {
  const { token } = useParams<{ token: string }>()
  const [data, setData] = useState<Redemption | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [check, setCheck] = useState<Check>({ state: 'checking' })

  useEffect(() => {
    if (!token) return
    apiGet<Redemption>(`/api/v1/redemption/${token}`)
      .then(setData)
      .catch((e) =>
        setError(e instanceof ApiError ? e.message : 'Could not load this code.'),
      )
  }, [token])

  const runCheck = useCallback(async (d: Redemption) => {
    try {
      // 1. Recompute the leaf from the opened commitment.
      const recomputed = await slotLeafHash(
        d.campaign_id,
        d.leaf_index,
        d.slot_token,
        d.ceiling_bps,
        d.salt_hex,
      )
      if (recomputed !== d.leaf_hash) {
        setCheck({
          state: 'bad',
          why: 'The leaf does not match the opened commitment.',
        })
        return
      }
      // 2. Walk it to the root the merchant published.
      const walk = await verifyProofWalk(
        recomputed,
        d.leaf_index,
        d.proof,
        d.merkle_root,
        d.tree_size,
      )
      if (!walk.ok) {
        setCheck({ state: 'bad', why: `Proof failed (${walk.failure}).` })
        return
      }
      // 3. And the promise itself.
      if (d.granted_bps != null && d.granted_bps > d.ceiling_bps) {
        setCheck({
          state: 'bad',
          why: 'The granted discount exceeds the committed ceiling.',
        })
        return
      }
      setCheck({ state: 'ok' })
    } catch (e) {
      setCheck({ state: 'bad', why: `Could not verify: ${String(e)}` })
    }
  }, [])

  useEffect(() => {
    if (data) void runCheck(data)
  }, [data, runCheck])

  if (error) {
    return (
      <main className="flex h-dvh items-center justify-center p-6">
        <p className="text-center text-sm text-ink-soft">{error}</p>
      </main>
    )
  }
  if (!data) {
    return (
      <main className="flex h-dvh items-center justify-center">
        <p className="text-sm text-ink-soft">Loading…</p>
      </main>
    )
  }

  return (
    <main className="mx-auto max-w-md space-y-4 p-4">
      <header>
        <h1 className="text-lg font-semibold text-ink">Show this at the counter</h1>
        <p className="text-sm text-ink-soft">
          {data.store} · {data.campaign_name}
        </p>
      </header>

      <section className="rounded-2xl border border-hairline bg-white p-4 text-center">
        <img
          src={`${API_BASE}/api/v1/redemption/${token}/qr.svg`}
          alt="Redemption code"
          className="mx-auto h-auto w-full max-w-[260px]"
        />
        <p className="mt-3 font-mono text-sm tracking-wider text-ink">
          {data.slot_token}
        </p>
        {data.granted_bps != null && (
          <p className="mt-2 text-sm text-ink-soft">
            {data.sku ?? 'item'} × {data.qty ?? 1} · {pct(data.granted_bps)} off
            {data.final_amount_paise != null &&
              ` · paid ${rupees(data.final_amount_paise)}`}
          </p>
        )}
      </section>

      <section
        className={`rounded-2xl border p-4 ${
          check.state === 'ok'
            ? 'border-pass/40 bg-pass/5'
            : check.state === 'bad'
              ? 'border-fail/40 bg-fail/5'
              : 'border-hairline bg-white'
        }`}
      >
        {check.state === 'checking' && (
          <p className="text-sm text-ink-soft">Verifying in your browser…</p>
        )}
        {check.state === 'ok' && (
          <>
            <p className="text-sm font-semibold text-pass">
              ✓ Verified in your browser
            </p>
            <p className="mt-1 text-xs leading-relaxed text-ink-soft">
              Your discount of {pct(data.granted_bps ?? 0)} is inside the{' '}
              {pct(data.ceiling_bps)} ceiling this shop committed to on{' '}
              {new Date(data.committed_at).toLocaleDateString()} — before you
              scanned. Your phone recomputed the leaf and walked the proof to
              the published root; nothing here was taken on trust.
            </p>
            <p className="mt-2 break-all font-mono text-[11px] text-ink-soft">
              root {data.merkle_root}
            </p>
          </>
        )}
        {check.state === 'bad' && (
          <>
            <p className="text-sm font-semibold text-fail">✗ Did not verify</p>
            <p className="mt-1 text-xs text-ink-soft">{check.why}</p>
          </>
        )}
      </section>

      {data.verified_at && (
        <p className="text-center text-xs text-ink-soft">
          Already scanned at {new Date(data.verified_at).toLocaleString()}.
        </p>
      )}
    </main>
  )
}
