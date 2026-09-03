import { useState } from 'react'
import type { Cart } from '../lib/api'
import { Money, Pct } from './ui'

/**
 * The basket, docked above the composer. The only place money is spent.
 *
 * Before this existed the Pay button lived on the offer card, which meant it
 * belonged to whichever item had been negotiated most recently — negotiate
 * atta, then oil, and the atta's button was gone along with the atta. One
 * button, one basket, one payment.
 *
 * COLLAPSED BY DEFAULT, and that is the important decision. This sits between
 * the conversation and the keyboard on a 360px phone, and the conversation is
 * the product. Collapsed it costs one line: how many items, what it comes to,
 * and Pay. Expanded it shows every line with the rate that line was granted,
 * which is the thing a shopper actually wants to check before paying —
 * "did the 6% on the rice survive?"
 *
 * What is NOT here, and must not come back: the ceiling. Same rule as the
 * offer card. Per-line savings are shown because the shopper already knows
 * them; headroom is not, because knowing it ends the negotiation.
 */
export default function CartPanel({
  cart,
  onPay,
  onRemove,
  paying = false,
  disabled = false,
}: {
  cart: Cart
  onPay: () => void
  onRemove?: (sku: string) => void
  paying?: boolean
  disabled?: boolean
}) {
  const [open, setOpen] = useState(false)
  if (cart.count === 0) return null

  return (
    <div className="border-t border-hairline bg-card">
      {open && (
        <ul className="max-h-[38dvh] divide-y divide-hairline overflow-y-auto px-4">
          {cart.items.map((line) => (
            <li key={line.sku} className="flex items-start gap-3 py-2.5">
              <div className="min-w-0 flex-1">
                <p className="truncate text-half font-medium text-ink">
                  {line.name}
                  {line.qty > 1 && (
                    <span className="text-ink-soft"> × {line.qty}</span>
                  )}
                </p>
                <p className="mt-0.5 text-xxs text-ink-soft">
                  {line.granted_bps > 0 ? (
                    <>
                      <span className="text-pass">
                        <Pct bps={line.granted_bps} className="font-sans" /> off
                      </span>
                      {' · saved '}
                      <Money paise={line.discount_paise} />
                    </>
                  ) : (
                    'at shelf price'
                  )}
                </p>
              </div>
              <div className="shrink-0 text-right">
                <p className="text-half font-medium text-ink">
                  <Money paise={line.line_total_paise} />
                </p>
                {onRemove && (
                  <button
                    type="button"
                    onClick={() => onRemove(line.sku)}
                    disabled={paying || disabled}
                    className="mt-0.5 text-xxs text-ink-soft underline underline-offset-2
                               transition-colors hover:text-fail disabled:opacity-40"
                  >
                    remove
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}

      <div className="flex items-center gap-3 px-4 py-2.5">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="min-w-0 flex-1 text-left"
        >
          <p className="text-xxs text-ink-soft">
            {cart.count} item{cart.count === 1 ? '' : 's'}
            {cart.discount_paise > 0 && (
              <>
                {' · saving '}
                <span className="text-pass">
                  <Money paise={cart.discount_paise} />
                </span>
              </>
            )}
            <span aria-hidden className="ml-1 text-2xs">
              {open ? '▼' : '▲'}
            </span>
          </p>
          <p className="font-display text-[19px] font-semibold leading-tight text-ink">
            <Money paise={cart.total_paise} className="font-display" />
          </p>
        </button>

        <button
          type="button"
          onClick={onPay}
          disabled={paying || disabled}
          className="shrink-0 rounded-xl bg-accent px-5 py-2.5 text-half font-semibold
                     text-white transition-colors hover:bg-accent-strong
                     disabled:bg-sunk disabled:text-ink-faint"
        >
          {paying ? 'Opening…' : 'Pay'}
        </button>
      </div>
    </div>
  )
}
