/**
 * The two small pieces of ceiling arithmetic the console does for itself.
 *
 * Everything else about a cap comes from the server: /simulate returns each
 * product's ceiling, and the same figure is what commit_campaign freezes and
 * the gate later enforces. These two functions exist only because the second
 * customer band is a display of a number the server has not been asked for --
 * the shopkeeper is moving a slider, not committing anything yet.
 *
 * That makes them the exact place a lie can appear: a page that shows a
 * ceiling the gate would never grant is worse than a page that shows nothing.
 * So they live here, next to their test, rather than inline in a component
 * where the arithmetic is invisible.
 */

/** Basis points are the money unit everywhere in this project; 10000 = 100%. */
export const MAX_BPS = 10_000

/**
 * A percentage typed into a box, as basis points -- or null when what is in
 * the box is not a number yet.
 *
 * Clamped, because customer_service.effective_cap_bps clamps:
 *
 *     fraction = max(0, min(cap_fraction_bps, MAX_BPS))
 *
 * Type 150 into "new customers get" and, without the clamp, the New column
 * would print a ceiling ABOVE the Regular one -- a number the gate can never
 * grant, on the page a shopkeeper is using to decide. Half-typed input
 * ("", "-", ".") returns null so a caller can show nothing rather than
 * quietly treat it as zero.
 */
export function bpsFromPct(text: string): number | null {
  const n = parseFloat(text)
  if (!Number.isFinite(n)) return null
  return Math.max(0, Math.min(Math.round(n * 100), MAX_BPS))
}

/**
 * A product's ceiling for a customer who does not qualify for the top band.
 *
 * The Python twin, which is what actually gates an offer:
 *
 *     if product_cap_bps <= 0: return 0
 *     fraction = max(0, min(cap_fraction_bps, MAX_BPS))
 *     return product_cap_bps * fraction // MAX_BPS
 *
 * Floor division in both, on non-negative integers, so the two agree exactly.
 * Truncation matters: a cap rounded UP by a single basis point is a cap the
 * server will refuse after the shopkeeper was shown it.
 */
export function bandCapBps(productCapBps: number, fractionBps: number): number {
  if (productCapBps <= 0) return 0
  const fraction = Math.max(0, Math.min(fractionBps, MAX_BPS))
  return Math.floor((productCapBps * fraction) / MAX_BPS)
}
