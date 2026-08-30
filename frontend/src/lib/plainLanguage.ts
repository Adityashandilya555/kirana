/**
 * The audit trail, in words a shopkeeper already knows.
 *
 * Every row in `decisions` carries two strings: `customer_reason`, written for
 * the person haggling, and `human_reason`, written for whoever is debugging.
 * The console was showing `human_reason` -- which is why the page read like
 *
 *     C02_SESSION_OPENED
 *     Session opened on slot BMWV67JR8M (ceiling 1200 bps, leaf 13).
 *
 * A shop owner does not know what a basis point is, has no use for a leaf
 * index, and cannot act on a Merkle root. None of that is wrong, and none of
 * it should be deleted -- it is the evidence, and an audit trail that quietly
 * drops its evidence is worth less than one nobody reads. So it moves behind a
 * "technical detail" toggle and this file supplies what sits in front.
 *
 * Two rules held throughout:
 *
 *   1. Never invent a number. Every figure here is read off a structured
 *      column (`granted_bps`, `proposed_bps`, `binding_constraint`). Nothing
 *      is parsed back out of `human_reason`, because a sentence that is
 *      reformatted by a regex is a sentence that will eventually lie.
 *
 *   2. Say what it means for the shop, not what the system did. "Held down to
 *      12% by this sticker's limit" beats "OK_CLAMPED_SLOT_CEILING", and the
 *      shopkeeper needs the first one to decide whether their limits are set
 *      right.
 */

import type { AuditRow } from './merchant'

/** Basis points are an internal unit. Nothing a shopkeeper reads shows them. */
export function pct(bps: number | null | undefined): string {
  if (bps == null) return '—'
  const v = bps / 100
  // 12, not 12.00 -- but 2.31 keeps its precision, because a margin floor
  // rounded to "2%" is a different promise from the one actually made.
  return `${Number.isInteger(v) ? v : Number(v.toFixed(2))}%`
}

/**
 * Which of the four limits actually bit.
 *
 * The column stores the variable name. A shop owner set these limits, but they
 * set them as "maximum discount" and "margin floor" on a form, so those are
 * the words they get back.
 */
export const LIMIT_LABEL: Record<string, string> = {
  slot_ceiling_bps: "this sticker's own limit",
  campaign_max_discount_bps: 'your maximum discount for this campaign',
  margin_floor_bps: 'your profit floor',
  budget_paise: 'the budget left in this campaign',
}

export function plainLimit(constraint: string | null | undefined): string {
  if (!constraint) return 'one of your limits'
  return LIMIT_LABEL[constraint] ?? constraint.replace(/_bps$|_paise$/, '').replace(/_/g, ' ')
}

export interface PlainRow {
  /** One sentence, readable without a glossary. */
  headline: string
  /** Optional second line: what it means for the shop's money or limits. */
  sub?: string
}

/**
 * Turn one decision row into plain English.
 *
 * Keyed on `kind` rather than `code` because `kind` is a closed set with a
 * CHECK constraint behind it (sql/001_schema.sql), while codes get added as
 * new reasons appear. Where a code changes the meaning materially -- a
 * released reservation, a second use of a code -- it is checked explicitly.
 */
export function plainRow(r: AuditRow): PlainRow {
  const granted = pct(r.granted_bps)
  const proposed = pct(r.proposed_bps)
  const limit = plainLimit(r.binding_constraint)

  switch (r.kind) {
    case 'campaign_committed':
      return {
        headline: 'Campaign locked in and stickers numbered.',
        sub: 'From this moment the limits cannot be changed — that is what lets a customer check you kept your word.',
      }

    case 'session_opened':
      return {
        headline: 'A customer scanned a sticker and started haggling.',
      }

    case 'injection_blocked':
      return {
        headline: 'Someone tried to talk the assistant out of your limits.',
        sub: 'Stopped before the AI was asked, so it never even saw the message. No discount was given.',
      }

    case 'tool_call':
      return { headline: 'The assistant looked something up.' }

    case 'proposal':
      return { headline: `The assistant suggested ${proposed} off.` }

    case 'approved':
      return {
        headline: `Agreed ${granted} off.`,
        sub: 'Inside every limit you set — nothing had to be held back.',
      }

    case 'clamped':
      return {
        headline: `Customer pushed for ${proposed}. They got ${granted}.`,
        sub: `Held down by ${limit}.`,
      }

    case 'rejected':
      return {
        headline: 'Refused — no discount at all.',
        sub: `There was no room left under ${limit}.`,
      }

    case 'llm_fallback':
      return {
        headline: 'The main AI did not answer, so the backup took over.',
        sub: 'Your limits applied exactly the same either way.',
      }

    case 'llm_error':
      return {
        headline: 'The AI was unavailable for this reply.',
        sub: 'The customer got a fixed response. No discount can be given when this happens.',
      }

    case 'order_created':
      return { headline: `Checkout opened at ${granted} off.` }

    case 'settled':
      return {
        headline: `Paid. You gave ${granted} off.`,
      }

    case 'payment_failed':
      return {
        headline:
          r.code === 'P02_RESERVATION_RELEASED'
            ? 'The customer did not finish paying.'
            : 'Payment did not go through.',
        sub: 'The money set aside for this discount went back into your budget.',
      }

    case 'verified':
      return {
        headline: 'Checked at your counter — genuine, and used for the first time.',
        sub: 'The discount given was inside the limit printed on that sticker. Safe to honour.',
      }

    case 'verify_rejected':
      return {
        headline: 'Someone tried to use this code a second time. Refused.',
        sub: 'A code works once. Do not give the discount again.',
      }

    default:
      // An unknown kind is a new one, not a broken one. Show the row's own
      // sentence rather than a blank space or a raw enum.
      return { headline: r.human_reason }
  }
}

/**
 * What a whole conversation came to.
 *
 * The thread header used to show an outcome enum. These are the same five
 * states said out loud, so the list can be skimmed for the ones that cost
 * money or need attention.
 */
export const OUTCOME_PLAIN: Record<string, string> = {
  blocked: 'Someone tried to cheat',
  settled: 'Bought and paid',
  refused: 'No discount given',
  offered: 'Offered, not bought',
  open: 'Still talking',
}
