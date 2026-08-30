/**
 * The plain-language layer has one failure mode worth guarding: a new decision
 * kind is added in SQL, nothing here knows about it, and the console quietly
 * falls back to printing `human_reason` -- the exact machine sentence this
 * file exists to replace. That regression is invisible in a screenshot,
 * because the page still renders something plausible.
 *
 * So KINDS below is a copy of the CHECK constraint in sql/001_schema.sql. If
 * the two drift, the last test fails and names the kind that has no sentence.
 */

import { describe, expect, it } from 'vitest'
import type { AuditRow } from './merchant'
import { LIMIT_LABEL, OUTCOME_PLAIN, pct, plainLimit, plainRow } from './plainLanguage'

/** sql/001_schema.sql: decisions.kind check constraint. */
const KINDS = [
  'campaign_committed',
  'session_opened',
  'injection_blocked',
  'tool_call',
  'proposal',
  'approved',
  'clamped',
  'rejected',
  'llm_fallback',
  'llm_error',
  'order_created',
  'settled',
  'payment_failed',
  'verified',
  'verify_rejected',
] as const

function row(over: Partial<AuditRow> = {}): AuditRow {
  return {
    id: 1,
    session_id: null,
    slot_id: null,
    turn_index: null,
    kind: 'approved',
    code: 'OK_AS_PROPOSED',
    proposed_bps: null,
    granted_bps: null,
    binding_constraint: null,
    human_reason: 'RAW MACHINE SENTENCE',
    customer_reason: null,
    llm_provider: null,
    llm_model: null,
    latency_ms: null,
    raw_user_message: null,
    created_at: '2026-08-30T08:56:45Z',
    ...over,
  }
}

// ------------------------------------------------------------------ pct ----
describe('pct', () => {
  it('drops meaningless decimals', () => {
    expect(pct(1200)).toBe('12%')
    expect(pct(500)).toBe('5%')
    expect(pct(2000)).toBe('20%')
  })

  it('keeps precision that changes the promise', () => {
    // A margin floor of 2.31% rounded to "2%" is a different commitment.
    expect(pct(231)).toBe('2.31%')
    expect(pct(1925)).toBe('19.25%')
  })

  it('never renders basis points or a bare null', () => {
    expect(pct(null)).toBe('—')
    expect(pct(1200)).not.toContain('bps')
  })
})

// ---------------------------------------------------------------- limits ----
describe('plainLimit', () => {
  it('names each limit the way the shopkeeper set it', () => {
    expect(plainLimit('slot_ceiling_bps')).toBe("this sticker's own limit")
    expect(plainLimit('margin_floor_bps')).toBe('your profit floor')
  })

  it('degrades to something readable for an unknown constraint', () => {
    const out = plainLimit('some_new_limit_bps')
    expect(out).not.toContain('_')
    expect(out).not.toContain('bps')
  })

  it('handles no constraint at all', () => {
    expect(plainLimit(null)).toBe('one of your limits')
  })
})

// ------------------------------------------------------------- sentences ----
describe('plainRow', () => {
  it('states the clamp as a before and after', () => {
    const p = plainRow(
      row({ kind: 'clamped', proposed_bps: 4000, granted_bps: 1200,
            binding_constraint: 'slot_ceiling_bps' }),
    )
    expect(p.headline).toContain('40%')
    expect(p.headline).toContain('12%')
    expect(p.sub).toContain("this sticker's own limit")
  })

  it('says the AI was never consulted on a blocked row', () => {
    const p = plainRow(row({ kind: 'injection_blocked' }))
    expect(p.sub).toMatch(/never even saw|never/i)
  })

  it('tells the shopkeeper the money came back when payment fails', () => {
    const p = plainRow(row({ kind: 'payment_failed', code: 'P02_RESERVATION_RELEASED' }))
    expect(p.headline).toContain('did not finish paying')
    expect(p.sub).toContain('back into your budget')
  })

  it('tells the counter not to honour a reused code', () => {
    const p = plainRow(row({ kind: 'verify_rejected' }))
    expect(p.sub).toContain('Do not give the discount again')
  })

  it('never leaks basis points, leaf indices or hashes', () => {
    for (const kind of KINDS) {
      const p = plainRow(
        row({ kind, proposed_bps: 4000, granted_bps: 1200,
              binding_constraint: 'slot_ceiling_bps' }),
      )
      const text = `${p.headline} ${p.sub ?? ''}`
      expect(text, kind).not.toMatch(/\bbps\b/)
      expect(text, kind).not.toMatch(/\bleaf\b/i)
      expect(text, kind).not.toMatch(/merkle|root [0-9a-f]{6}/i)
    }
  })

  it('has a sentence for every kind the database can produce', () => {
    for (const kind of KINDS) {
      const p = plainRow(row({ kind }))
      // Falling through to the raw column is the regression this guards.
      expect(p.headline, `no plain sentence for kind "${kind}"`).not.toBe(
        'RAW MACHINE SENTENCE',
      )
      expect(p.headline.length, kind).toBeGreaterThan(0)
    }
  })

  it('still shows something for a kind it has never seen', () => {
    const p = plainRow(row({ kind: 'something_new' }))
    expect(p.headline).toBe('RAW MACHINE SENTENCE')
  })
})

// -------------------------------------------------------------- outcomes ----
describe('OUTCOME_PLAIN', () => {
  it('covers every outcome the thread grouper produces', () => {
    for (const o of ['blocked', 'settled', 'refused', 'offered', 'open']) {
      expect(OUTCOME_PLAIN[o], o).toBeTruthy()
    }
  })
})

describe('LIMIT_LABEL', () => {
  /*
   * These keys are the VALUES of BINDING in backend/app/core/codes.py, and
   * this list previously got one of them wrong: it asserted `budget_paise`
   * while the backend has always sent `remaining_budget_paise`. Because the
   * label map agreed with the test rather than with the backend, both were
   * green and every budget-bound clamp silently rendered the generic fallback.
   *
   * A test that agrees with the wrong implementation is worse than no test.
   * The real guard is now on the Python side — test_new_ceilings.py reads this
   * file and asserts the two sets are identical — so drift fails loudly rather
   * than being confirmed by a copy of itself. This stays as a fast local check
   * that nothing is missing.
   */
  it('covers every bound the gate can be held by', () => {
    for (const k of [
      'product_cap_bps',
      'slot_ceiling_bps',
      'campaign_max_discount_bps',
      'margin_floor_bps',
      'remaining_budget_paise',
      'customer_tier_cap_bps',
    ]) {
      expect(LIMIT_LABEL[k], k).toBeTruthy()
    }
  })

  it('says which limit bit, without naming a number', () => {
    // The band label in particular reaches a shopkeeper's screen next to the
    // conversation. It should describe the rule, not quote its ceiling.
    for (const label of Object.values(LIMIT_LABEL)) {
      expect(label).not.toMatch(/\d/)
    }
  })
})
