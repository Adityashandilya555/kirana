import { API_BASE, ApiError, MERCHANT_KEY, apiGet, apiPost } from './api'

/** Everything the shopkeeper's console talks to. All merchant-key protected. */

// ---------------------------------------------------------------- catalog --
export interface CatalogItem {
  sku: string
  name: string
  unit: string
  price_paise: number
  cost_paise: number
  active: boolean
  margin_bps: number
}

export interface ParsedRow {
  line: number
  sku: string
  name: string
  unit: string
  price_paise: number
  cost_paise: number
  margin_bps: number
  ok: boolean
  errors: string[]
}

export interface ParsedSheet {
  mapping: Record<string, string>
  missing_columns: string[]
  total: number
  accepted: number
  rejected: number
  rows: ParsedRow[]
  imported?: { upserted: number; deactivated: number }
}

/** Multipart upload. Not apiPost — that sets a JSON content-type, and setting
 *  any content-type by hand on FormData strips the multipart boundary the
 *  browser generates, which the server then cannot parse. */
async function upload<T>(path: string, form: FormData): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'X-Merchant-Key': MERCHANT_KEY },
    body: form,
  })
  if (res.ok) return res.json() as Promise<T>
  let code = `HTTP_${res.status}`
  let message = res.statusText
  try {
    const detail = (await res.json())?.detail
    if (detail && typeof detail === 'object') {
      code = detail.code ?? code
      message = detail.message ?? message
    }
  } catch {
    /* a proxy error page is not JSON */
  }
  throw new ApiError(res.status, code, message)
}

export const getCatalog = () =>
  apiGet<{ items: CatalogItem[] }>('/api/v1/catalog', true)

export const previewSheet = (file: File) => {
  const f = new FormData()
  f.append('file', file)
  return upload<ParsedSheet>('/api/v1/catalog/preview', f)
}

export const importSheet = (file: File, replace: boolean) => {
  const f = new FormData()
  f.append('file', file)
  f.append('replace', String(replace))
  return upload<ParsedSheet>('/api/v1/catalog/import', f)
}

export const saveItems = (items: unknown[]) =>
  apiPost<{ upserted: number }>('/api/v1/catalog/items', { items }, true)

// ----------------------------------------------------------------- shelves --
export interface Shelf {
  id: string
  name: string
  note: string
  skus: string[]
  item_count: number
}

export const getShelves = () =>
  apiGet<{ shelves: Shelf[] }>('/api/v1/shelves', true)

export const saveShelf = (body: {
  name: string
  skus: string[]
  note?: string
  shelf_id?: string | null
}) => apiPost<{ shelf_id: string }>('/api/v1/shelves', body, true)

export async function deleteShelf(id: string) {
  const res = await fetch(`${API_BASE}/api/v1/shelves/${id}`, {
    method: 'DELETE',
    headers: { 'X-Merchant-Key': MERCHANT_KEY },
  })
  if (!res.ok) throw new ApiError(res.status, 'DELETE_FAILED', 'Could not remove that shelf.')
  return res.json()
}

// --------------------------------------------------------------- campaigns --
export interface Campaign {
  id: string
  name: string
  status: string
  budget_paise: number
  spent_paise: number
  reserved_paise: number
  remaining_paise: number
  max_discount_bps: number
  margin_floor_bps: number
  max_turns: number
  slot_count: number
  slot_binding: 'open' | 'product' | 'shelf'
  merkle_root: string | null
  tree_size: number | null
  committed_at: string | null
  created_at: string
  slots_total: number
  slots_redeemed: number
  slots_verified: number
  scopes: { bound_sku: string | null; shelf_id: string | null; shelf_name: string | null; slots: number }[]
  merchant: { id: string; name: string; store_line: string }
}

export const getCampaigns = () =>
  apiGet<Campaign[]>(
    '/api/v1/campaigns?merchant_id=00000000-0000-0000-0000-00000000d001',
    true,
  ).catch(() => [] as Campaign[])

export const getCampaign = (id: string) =>
  apiGet<Campaign>(`/api/v1/campaigns/${id}`, true)

export const createCampaign = (body: Record<string, unknown>) =>
  apiPost<Campaign>('/api/v1/campaigns', body, true)

export const commitCampaign = (id: string, targets: string[]) =>
  apiPost<Campaign & { slots_created: number; qr_sheet_url: string }>(
    `/api/v1/campaigns/${id}/commit`,
    { targets },
    true,
  )

export interface Slot {
  leaf_index: number
  slot_token: string
  ceiling_bps: number
  status: string
  leaf_hash: string
}

export const getSlots = (id: string) =>
  apiGet<Slot[]>(`/api/v1/campaigns/${id}/slots`, true)

export const qrSheetUrl = (id: string) =>
  `${API_BASE}/api/v1/campaigns/${id}/qr-sheet?k=${encodeURIComponent(MERCHANT_KEY)}`

// -------------------------------------------------------------- simulator --
export interface SimItem {
  sku: string
  name: string
  price_paise: number
  max_discount_bps: number
  max_discount_pct: number
  margin_at_list_pct: number
  discountable: boolean
  binding: string
  explain: string
}

export interface Simulation {
  items: SimItem[]
  blocked_skus: string[]
  blocked_count: number
  discountable_count: number
  ceiling_tiers: { ceiling_bps: number; ceiling_pct: number; slots: number }[]
  budget: {
    budget_paise: number
    worst_case_total_paise: number
    covers_all_slots: boolean
    slots_before_exhausted: number
    slot_count: number
  }
  warnings: { level: 'info' | 'warn' | 'stop'; message: string }[]
}

export const simulate = (body: {
  max_discount_bps: number
  margin_floor_bps: number
  budget_paise: number
  slot_count: number
}) => apiPost<Simulation>('/api/v1/simulate', body, true)

// ------------------------------------------------------------------ audit --
export interface AuditRow {
  id: number
  session_id: string | null
  slot_id: string | null
  turn_index: number | null
  kind: string
  code: string
  proposed_bps: number | null
  granted_bps: number | null
  binding_constraint: string | null
  human_reason: string
  customer_reason: string | null
  llm_provider: string | null
  llm_model: string | null
  latency_ms: number | null
  raw_user_message: string | null
  created_at: string
}

export interface AuditFeed {
  cursor: number
  campaign: Campaign
  items: AuditRow[]
}

export const getAudit = (id: string, afterId: number) =>
  apiGet<AuditFeed>(`/api/v1/campaigns/${id}/audit?after_id=${afterId}&limit=200`, true)

/** Per-conversation context the decision rows don't carry — above all, which
 *  products the slot's scope kept out of the model's world. */
export interface SessionAudit {
  session_id: string
  started_at: string
  status: string
  turn_count: number
  slot_token: string
  ceiling_bps: number
  slot_status: string
  bound_sku: string | null
  shelf_name: string | null
  sku: string | null
  qty: number
  offer_bps: number | null
  amount_paise: number | null
  visible_skus: string[]
  withheld_skus: string[]
}

export const getSessionAudit = (id: string) =>
  apiGet<SessionAudit[]>(`/api/v1/campaigns/${id}/sessions`, true)
