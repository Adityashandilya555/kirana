import { apiGet } from './api'

/**
 * Razorpay Standard Checkout.
 *
 * The script loader is promise-cached: checkout.js is idempotent but a second
 * <script> tag while the first is still in flight gives you two loads and a
 * race on window.Razorpay. One promise, reused.
 *
 * Card is forced to the top of the block sequence. UPI Collect is unsupported
 * on Android per NPCI guidance and the demo handset is Android, so a customer
 * who taps UPI first hits a dead end in front of an audience. The rehearsed
 * path is 4111 1111 1111 1111, any future expiry, any CVV, OTP 1234.
 *
 * Three exits have to be handled, not one. `handler` fires on success,
 * `payment.failed` on a declined card, and `modal.ondismiss` when the customer
 * closes the sheet — and only the first of those is on the happy path. Miss
 * the other two and the reservation silently holds discount for the rest of
 * the demo.
 */

const SCRIPT_SRC = 'https://checkout.razorpay.com/v1/checkout.js'

let loader: Promise<void> | null = null

export function loadCheckout(): Promise<void> {
  if (window.Razorpay) return Promise.resolve()
  if (loader) return loader
  loader = new Promise<void>((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>(
      `script[src="${SCRIPT_SRC}"]`,
    )
    if (existing) {
      existing.addEventListener('load', () => resolve())
      existing.addEventListener('error', () => reject(new Error('checkout.js failed')))
      return
    }
    const el = document.createElement('script')
    el.src = SCRIPT_SRC
    el.async = true
    el.onload = () => resolve()
    el.onerror = () => {
      loader = null // let a later attempt retry rather than caching the failure
      reject(new Error('Could not load the payment sheet.'))
    }
    document.head.appendChild(el)
  })
  return loader
}

export interface AcceptedOrder {
  order_id: string
  amount_paise: number
  currency: string
  key_id: string | null
  stub: boolean
  sku: string
  qty: number
  discount_paise: number
  prefill?: { name?: string; email?: string; contact?: string }
  slot_token: string
  /** Present on a basket checkout. One order can now cover several items, so
   *  the sheet says "3 items" rather than naming whichever line happened to
   *  be largest. */
  line_count?: number
}

export interface CheckoutResult {
  order_id: string
  payment_id: string
  signature: string | null
}

interface RazorpayInstance {
  open: () => void
  on: (event: string, cb: (payload: unknown) => void) => void
}

declare global {
  interface Window {
    Razorpay?: new (options: Record<string, unknown>) => RazorpayInstance
  }
}

export async function openCheckout(
  order: AcceptedOrder,
  merchantName: string,
): Promise<CheckoutResult | { dismissed: true } | { failed: string }> {
  await loadCheckout()
  if (!window.Razorpay) throw new Error('Payment sheet unavailable.')
  if (!order.key_id) throw new Error('Payments are not configured for this shop.')

  return new Promise((resolve) => {
    let settled = false
    const once = (v: CheckoutResult | { dismissed: true } | { failed: string }) => {
      if (!settled) {
        settled = true
        resolve(v)
      }
    }

    const rzp = new window.Razorpay!({
      key: order.key_id,
      amount: order.amount_paise,
      currency: order.currency,
      order_id: order.order_id,
      name: merchantName,
      description:
        order.line_count && order.line_count > 1
          ? `${order.line_count} items`
          : `${order.sku} × ${order.qty}`,
      prefill: order.prefill ?? {},
      theme: { color: '#3a3f8f' },
      // Card first: UPI Collect is blocked on Android by NPCI rules.
      config: {
        display: {
          blocks: { card: { name: 'Pay by card', instruments: [{ method: 'card' }] } },
          sequence: ['block.card'],
          preferences: { show_default_blocks: true },
        },
      },
      handler: (response: {
        razorpay_payment_id: string
        razorpay_order_id: string
        razorpay_signature: string
      }) =>
        once({
          order_id: response.razorpay_order_id,
          payment_id: response.razorpay_payment_id,
          signature: response.razorpay_signature,
        }),
      modal: { ondismiss: () => once({ dismissed: true }) },
    })

    rzp.on('payment.failed', (payload: unknown) => {
      const desc =
        (payload as { error?: { description?: string } })?.error?.description ??
        'The payment did not go through.'
      once({ failed: desc })
    })

    rzp.open()
  })
}

/**
 * Polling backup for when the checkout handler never fires — a browser killed
 * mid-redirect, a dropped connection on the callback. 2s while the payment is
 * fresh, backing off after 30s, hard stop at 90s so a forgotten tab does not
 * poll forever.
 */
export async function pollUntilSettled(
  orderId: string,
  sessionId: string,
  onTick?: (attempt: number) => void,
): Promise<Record<string, unknown> | null> {
  const started = Date.now()
  let attempt = 0
  while (Date.now() - started < 90_000) {
    attempt += 1
    onTick?.(attempt)
    try {
      const res = await apiGet<Record<string, unknown>>(
        // session_id proves this order is ours: the order id alone appears in
        // the checkout sheet and in Razorpay's dashboard, and the response
        // carries the redemption token.
        `/api/v1/payments/${orderId}/status?session_id=${encodeURIComponent(sessionId)}`,
      ).catch(() => null)
      if (res && res.settled) return res
    } catch {
      // A transient failure is not a verdict; keep polling until the cap.
    }
    const elapsed = Date.now() - started
    await new Promise((r) => setTimeout(r, elapsed > 30_000 ? 5_000 : 2_000))
  }
  return null
}
