// Everything the browser needs goes through the FastAPI backend.
// The frontend never talks to Supabase directly: *.supabase.co was
// DNS-blocked by Indian ISPs for ~8 days in Feb-Mar 2026, and the phone is
// the one device we cannot control the network of.
export const API_BASE = (
  import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'
).replace(/\/$/, '')

export const MERCHANT_KEY = import.meta.env.VITE_MERCHANT_API_KEY ?? ''

export async function apiGet<T>(path: string, merchant = false): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: merchant ? { 'X-Merchant-Key': MERCHANT_KEY } : {},
  })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json() as Promise<T>
}
