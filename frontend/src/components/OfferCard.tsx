import { useState } from 'react'
import type { Offer } from '../lib/api'
import { Money, Pct } from './ui'

/**
 * One negotiation, as a shopper sees it: what this item now costs.
 *
 * It used to carry the Pay button, and that was the bug. A card with its own
 * Pay button is a card that claims to be the whole order — so negotiating a
 * second item silently replaced the first, and the only thing a shopper could
 * buy was whatever they had haggled most recently. The button now lives on
 * the basket, once, and this card is the receipt for one line in it.
 *
 * The mechanism is deliberately one tap away rather than on the face of the
 * card. A judge holding the phone can open it and see asked-versus-granted and
 * the rule that bound; a shopper never has to. Putting the audit detail on the
 * card by default would make the conversation look like a compliance form,
 * which is the opposite of the pitch.
 *
 * What is NOT here, and must not come back: the ceiling. For a typical sticker
 * the gate's max_allowed_bps is exactly the slot's committed limit, so showing
 * it ends the negotiation — the shopper stops haggling and asks for the number
 * on screen. It stays server-side where the model needs it.
 */

const BOUND_LABEL: Record<string, string> = {
  slot_ceiling_bps: 'this code’s shelf limit',
  campaign_max_discount_bps: 'the campaign maximum',
  margin_floor_bps: 'the margin floor',
  remaining_budget_paise: 'the remaining promo budget',
}

export default function OfferCard({
  offer,
  itemName,
}: {
  offer: Offer
  itemName?: string
}) {
  const [open, setOpen] = useState(false)
  const bound = offer.binding_constraint
    ? (BOUND_LABEL[offer.binding_constraint] ?? offer.binding_constraint)
    : null

  return (
    <div className="rounded-2xl border border-hairline bg-card p-5 shadow-card">
      <div className="flex items-baseline justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-tiny text-ink-soft">{itemName ?? offer.sku}</p>
          <p className="font-display text-[30px] font-medium leading-none text-ink">
            <Money paise={offer.final_amount_paise} className="font-display" />
          </p>
        </div>
        <div className="shrink-0 text-right">
          <p className="text-half font-semibold text-pass">
            <Pct bps={offer.granted_bps} className="font-sans" /> off
          </p>
          <p className="text-xxs text-ink-soft">
            saves <Money paise={offer.discount_paise} />
          </p>
        </div>
      </div>

      {offer.capped && (
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="mt-4 inline-flex items-center gap-1.5 rounded-full border border-hairline
                     bg-surface px-3 py-1 text-xxs font-medium text-ink-soft
                     transition-colors hover:bg-sunk"
        >
          capped by {bound ?? 'a shelf limit'}
          <span aria-hidden className="text-2xs">{open ? '▲' : '▼'}</span>
        </button>
      )}

      {open && (
        <dl className="mt-4 space-y-1.5 border-t border-hairline pt-4 text-tiny text-ink-soft">
          <div className="flex justify-between gap-4">
            <dt>the assistant asked for</dt>
            <dd><Pct bps={offer.proposed_bps} /></dd>
          </div>
          <div className="flex justify-between gap-4">
            <dt>the shop granted</dt>
            <dd className="text-pass"><Pct bps={offer.granted_bps} /></dd>
          </div>
          <div className="flex justify-between gap-4">
            <dt>held by</dt>
            <dd className="font-mono text-2xs">{offer.binding_constraint ?? '—'}</dd>
          </div>
          <p className="pt-2 leading-relaxed">
            That limit was committed to a Merkle root before this sticker was
            printed. It cannot be raised without the root changing.
          </p>
        </dl>
      )}

      {offer.added_to_cart && (
        /* A quiet confirmation, not a call to action. The shopper's next move
           is to keep talking; the Pay button is on the basket below and stays
           there however many things they add. */
        <p className="mt-4 flex items-center gap-1.5 text-xxs font-medium text-pass">
          <span aria-hidden>✓</span> Added to your basket
        </p>
      )}
    </div>
  )
}
