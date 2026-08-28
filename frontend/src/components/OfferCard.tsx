import { useState } from 'react'
import type { Offer } from '../lib/api'
import { pct, rupees } from '../lib/api'

/**
 * The offer, as a shopper sees it: a price and a button.
 *
 * The mechanism is deliberately ONE TAP away rather than on the face of the
 * card. A judge holding the phone can open it and see proposed-vs-granted and
 * the rule that bound; a shopper never has to. Putting the audit detail on the
 * card by default would make the conversation look like a compliance form,
 * which is the opposite of the pitch.
 */

const BOUND_LABEL: Record<string, string> = {
  slot_ceiling_bps: "this code's shelf limit",
  campaign_max_discount_bps: 'the campaign maximum',
  margin_floor_bps: 'the margin floor',
  remaining_budget_paise: 'the remaining promo budget',
}

export default function OfferCard({
  offer,
  itemName,
  onAccept,
  accepting = false,
}: {
  offer: Offer
  itemName?: string
  onAccept?: () => void
  accepting?: boolean
}) {
  const [open, setOpen] = useState(false)
  const bound = offer.binding_constraint
    ? (BOUND_LABEL[offer.binding_constraint] ?? offer.binding_constraint)
    : null

  return (
    <div className="rounded-2xl border border-hairline bg-white p-4 shadow-sm">
      <div className="flex items-baseline justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-sm text-ink-soft">{itemName ?? offer.sku}</p>
          <p className="text-2xl font-semibold text-ink">
            {rupees(offer.final_amount_paise)}
          </p>
        </div>
        <div className="shrink-0 text-right">
          <p className="text-sm font-medium text-pass">{pct(offer.granted_bps)} off</p>
          <p className="text-xs text-ink-soft">saves {rupees(offer.discount_paise)}</p>
        </div>
      </div>

      {offer.capped && (
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="mt-3 inline-flex items-center gap-1 rounded-full border border-hairline
                     px-2.5 py-1 text-xs text-ink-soft active:bg-slate-50"
        >
          capped by {bound ?? 'a shelf limit'}
          <span aria-hidden className="text-[10px]">{open ? '▲' : '▼'}</span>
        </button>
      )}

      {open && (
        <dl className="mt-3 space-y-1 border-t border-hairline pt-3 text-xs text-ink-soft">
          {/* Deliberately no "most this code allows" row. That number is the
              slot's committed ceiling, and showing it ends the negotiation:
              the shopper stops haggling and asks for it. Asked-vs-granted plus
              the rule that bound still shows the gate doing its work. */}
          <div className="flex justify-between gap-4">
            <dt>agent asked for</dt>
            <dd className="font-mono">{pct(offer.proposed_bps)}</dd>
          </div>
          <div className="flex justify-between gap-4">
            <dt>granted</dt>
            <dd className="font-mono text-pass">{pct(offer.granted_bps)}</dd>
          </div>
          <div className="flex justify-between gap-4">
            <dt>bound by</dt>
            <dd className="font-mono">{offer.binding_constraint ?? '—'}</dd>
          </div>
          <p className="pt-1 leading-relaxed">
            The ceiling was committed to a Merkle root before this code was
            printed. It cannot be raised without the root changing.
          </p>
        </dl>
      )}

      {onAccept && (
        <button
          type="button"
          onClick={onAccept}
          disabled={accepting}
          className="mt-4 w-full rounded-xl bg-accent py-3 text-sm font-semibold
                     text-white disabled:opacity-60"
        >
          {accepting ? 'Opening checkout…' : `Pay ${rupees(offer.final_amount_paise)}`}
        </button>
      )}
    </div>
  )
}
