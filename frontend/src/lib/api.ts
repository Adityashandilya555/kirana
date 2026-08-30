// Everything the browser needs goes through the FastAPI backend.
// The frontend never talks to Supabase directly: *.supabase.co was
// DNS-blocked by Indian ISPs for ~8 days in Feb-Mar 2026, and the phone is
// the one device we cannot control the network of.
export const API_BASE = (
  import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'
).replace(/\/$/, '')

export const MERCHANT_KEY = import.meta.env.VITE_MERCHANT_API_KEY ?? ''

/** The backend's error shape: {"detail":{"code","message"}}. */
export class ApiError extends Error {
  // Written out rather than as constructor parameter properties: the app
  // tsconfig sets erasableSyntaxOnly, which forbids TS-only syntax that a
  // plain type-strip cannot remove.
  status: number
  code: string

  constructor(status: number, code: string, message: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

async function parse<T>(res: Response): Promise<T> {
  if (res.ok) return res.json() as Promise<T>
  let code = `HTTP_${res.status}`
  let message = res.statusText
  try {
    const body = await res.json()
    const detail = body?.detail
    if (detail && typeof detail === 'object') {
      code = detail.code ?? code
      message = detail.message ?? message
    }
  } catch {
    // A proxy error page is not JSON. The status line is all we get.
  }
  throw new ApiError(res.status, code, message)
}

export async function apiGet<T>(path: string, merchant = false): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: merchant ? { 'X-Merchant-Key': MERCHANT_KEY } : {},
  })
  return parse<T>(res)
}

export async function apiPost<T>(
  path: string,
  body: unknown,
  merchant = false,
): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      ...(merchant ? { 'X-Merchant-Key': MERCHANT_KEY } : {}),
    },
    body: JSON.stringify(body),
  })
  return parse<T>(res)
}

// --- shapes the backend actually returns ----------------------------------

export interface CatalogItem {
  sku: string
  name: string
  unit: string
  price_paise: number
}

export interface Turn {
  role: 'user' | 'assistant' | 'system'
  content: string
  at?: string
}

export interface SessionPayload {
  session_id: string
  resumed: boolean
  session: { status: string; turn_count: number }
  slot: { slot_token: string; status: string }
  campaign: { name: string; status: string; max_turns: number }
  merchant: { name: string; store_line: string }
  catalog: CatalogItem[]
  transcript: Turn[]
  /** Only ever the last four digits — the number the browser sent is never
   *  echoed back, so a shared screen cannot leak it. */
  customer: {
    identified: boolean
    phone_last4: string | null
    returning: boolean
  }
  /** A number was given and could not be read. The page says so rather than
   *  silently treating a returning customer as a stranger. */
  phone_unreadable: boolean
}

export interface Offer {
  sku: string
  qty: number
  granted_bps: number
  proposed_bps: number
  discount_paise: number
  final_amount_paise: number
  code: string
  /** True when the gate clamped: what the "capped" chip keys off. */
  capped: boolean
  binding_constraint: string | null
  customer_reason: string
  // No max_allowed_bps. For a typical sticker that value IS the slot's
  // committed ceiling, and a shopper who can read it stops negotiating and
  // just asks for it. It stays server-side, where the model needs it.
}

export interface ChatReply {
  session_id: string
  reply: string
  offer: Offer | null
  blocked: boolean
  block_categories?: string[]
  turn_limit_reached?: boolean
  provider: string | null
  model?: string | null
  latency_ms: number
  steps?: number
  turn_count: number
  max_turns?: number
}

export const rupees = (paise: number) =>
  `₹${(paise / 100).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

export const pct = (bps: number) => `${(bps / 100).toFixed(bps % 100 === 0 ? 0 : 2)}%`
