import { describe, expect, it } from 'vitest'
import { MAX_BPS, bandCapBps, bpsFromPct } from './caps'

/**
 * The Python twin these are pinned against, in
 * backend/app/services/customer_service.py:
 *
 *     def effective_cap_bps(product_cap_bps: int, cap_fraction_bps: int) -> int:
 *         if product_cap_bps <= 0:
 *             return 0
 *         fraction = max(0, min(cap_fraction_bps, MAX_BPS))
 *         return product_cap_bps * fraction // MAX_BPS
 *
 * Reimplemented here rather than imported, because the point of the test is
 * that two independent implementations agree. Importing the answer would
 * prove nothing.
 */
function pythonEffectiveCapBps(productCapBps: number, capFractionBps: number): number {
  if (productCapBps <= 0) return 0
  const fraction = Math.max(0, Math.min(capFractionBps, MAX_BPS))
  return Math.floor((productCapBps * fraction) / MAX_BPS)
}

describe('bpsFromPct', () => {
  it('reads a percentage as basis points', () => {
    expect(bpsFromPct('20')).toBe(2000)
    expect(bpsFromPct('12.5')).toBe(1250)
    expect(bpsFromPct('0')).toBe(0)
  })

  it('returns null for input that is not a number yet', () => {
    // Every one of these is reachable by typing into the box.
    expect(bpsFromPct('')).toBeNull()
    expect(bpsFromPct('-')).toBeNull()
    expect(bpsFromPct('.')).toBeNull()
    expect(bpsFromPct('abc')).toBeNull()
  })

  it('clamps above 100% and below zero, as the server does', () => {
    // The bug this function exists for: 150% would have printed a New
    // ceiling above the Regular one.
    expect(bpsFromPct('150')).toBe(MAX_BPS)
    expect(bpsFromPct('-5')).toBe(0)
  })
})

describe('bandCapBps', () => {
  it('halves a ceiling for the lower band', () => {
    expect(bandCapBps(2000, 5000)).toBe(1000)
  })

  it('is the identity at a full fraction', () => {
    expect(bandCapBps(1935, MAX_BPS)).toBe(1935)
  })

  it('is zero for a product that cannot be discounted', () => {
    expect(bandCapBps(0, 5000)).toBe(0)
    expect(bandCapBps(-1, 5000)).toBe(0)
  })

  it('truncates rather than rounds up', () => {
    // 231 * 0.5 = 115.5. Rounding gives 116, which is a basis point the
    // server would refuse after the shopkeeper had been shown it.
    expect(bandCapBps(231, 5000)).toBe(115)
  })

  it('agrees with the Python across a grid', () => {
    for (let cap = 0; cap <= 2500; cap += 7) {
      for (let frac = 0; frac <= MAX_BPS; frac += 137) {
        expect(bandCapBps(cap, frac)).toBe(pythonEffectiveCapBps(cap, frac))
      }
    }
  })
})
