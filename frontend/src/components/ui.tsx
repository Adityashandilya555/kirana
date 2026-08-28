import type { ReactNode } from 'react'

/**
 * The recipes every surface is built from.
 *
 * These exist because the design was ported from an export written entirely in
 * inline style objects -- 1,100 lines with zero className. Translating that
 * per-component would have produced seven slightly different cards. Naming the
 * recipes once means a change to the card shape is one edit, and it keeps the
 * geometry consistent across fourteen screens.
 *
 * Now cut to Z-Matrix geometry (Matrix-bd): 10px radius on cards and buttons,
 * 36px control height, one soft slate shadow. Depth is inset-versus-elevated
 * rather than stacked shadow -- white cards on the canvas carry `shadow-card`,
 * and panels *inside* those cards go darker (sunk) rather than lighter. Two
 * levels, no more.
 */

/* ------------------------------------------------------------------ card -- */

export function Card({
  children,
  className = '',
  padded = true,
}: {
  children: ReactNode
  className?: string
  padded?: boolean
}) {
  return (
    <section
      className={[
        'rounded-xl border border-hairline bg-card shadow-card',
        padded ? 'p-5 md:p-6' : 'overflow-hidden',
        className,
      ].join(' ')}
    >
      {children}
    </section>
  )
}

export function CardHeader({
  title,
  sub,
  actions,
}: {
  title: ReactNode
  sub?: ReactNode
  actions?: ReactNode
}) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-3 border-b border-hairline px-6 py-4">
      <div className="min-w-0">
        <h2 className="font-display text-[15px] font-bold leading-tight tracking-[-0.01em] text-ink">
          {title}
        </h2>
        {sub && <p className="mt-1 text-mini text-ink-soft">{sub}</p>}
      </div>
      {actions && <div className="flex shrink-0 gap-2">{actions}</div>}
    </header>
  )
}

/* --------------------------------------------------------------- eyebrow -- */

/** The most repeated typographic device in the design: a small uppercase
 *  label above a number or a section. Z-Matrix sets these at 0.14em -- wide
 *  enough that 10px type still reads as a deliberate label, not small print. */
export function Eyebrow({ children }: { children: ReactNode }) {
  return (
    <span className="block text-2xs font-semibold uppercase tracking-[0.14em] text-ink-soft">
      {children}
    </span>
  )
}

/* --------------------------------------------------------------- buttons -- */

type Variant = 'primary' | 'ghost' | 'danger' | 'accent'

const VARIANTS: Record<Variant, string> = {
  // Slate on white text. The old palette could not do this -- turquoise behind
  // white is ~1.6:1 -- so primary had to be near-black ink. #496580 carries
  // white at 5.9:1, which is what lets the accent finally be the CTA.
  primary:
    'bg-accent text-white shadow-card hover:bg-accent-strong hover:shadow-lift active:bg-accent-press',
  // Z-Matrix "secondary": a white surface with a hairline, not a tinted block.
  ghost: 'border border-hairline bg-card text-ink shadow-card hover:bg-sunk',
  // Danger is an outline, not a filled red block: destructive actions should
  // be findable, not loud enough to be clicked by reflex.
  danger: 'border border-fail-line bg-card text-fail hover:bg-fail-bg',
  // The quiet accent: a tinted well for a secondary affirmative.
  accent: 'border border-accent-line bg-accent-soft text-accent hover:bg-accent-line/50',
}

export function Button({
  children,
  onClick,
  type = 'button',
  variant = 'primary',
  disabled,
  title,
  full,
}: {
  children: ReactNode
  onClick?: () => void
  type?: 'button' | 'submit'
  variant?: Variant
  disabled?: boolean
  title?: string
  full?: boolean
}) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={[
        'inline-flex min-h-9 items-center justify-center gap-2 rounded-xl px-4 py-2',
        'text-mini font-semibold tracking-[-0.005em]',
        'transition-[background-color,box-shadow,color] duration-200 ease-[cubic-bezier(0.2,0.7,0.2,1)]',
        'disabled:pointer-events-none disabled:border-hairline disabled:bg-sunk',
        'disabled:text-ink-faint disabled:shadow-none',
        full ? 'w-full' : '',
        VARIANTS[variant],
      ].join(' ')}
    >
      {children}
    </button>
  )
}

/* ---------------------------------------------------------------- inputs -- */

/** A field reads as a well cut into the white card rather than a box drawn on
 *  top of it: sunken ground, hairline border, and the accent only on focus. */
export const inputClass =
  'w-full rounded-xl border border-hairline bg-surface px-3.5 py-2 min-h-9 ' +
  'text-mini text-ink outline-none placeholder:text-ink-faint ' +
  'transition-colors duration-200 ease-[cubic-bezier(0.2,0.7,0.2,1)] ' +
  'hover:border-hairline-strong focus:border-accent focus:bg-card'

export function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: ReactNode
  children: ReactNode
}) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-mini font-semibold text-ink">{label}</span>
      {children}
      {hint && <span className="mt-1.5 block text-tiny text-ink-soft">{hint}</span>}
    </label>
  )
}

/* ----------------------------------------------------------------- pills -- */

export type Tone = 'pass' | 'fail' | 'warn' | 'neutral' | 'accent'

const TONES: Record<Tone, string> = {
  pass: 'bg-pass-bg text-pass border-pass-line',
  fail: 'bg-fail-bg text-fail border-fail-line',
  warn: 'bg-warn-bg text-warn border-warn-line',
  neutral: 'bg-sunk text-ink-2 border-hairline',
  accent: 'bg-accent-soft text-accent border-accent-line',
}

export function Pill({ tone = 'neutral', dot, children }: {
  tone?: Tone
  dot?: boolean
  children: ReactNode
}) {
  return (
    <span className={`chip ${TONES[tone]}`}>
      {dot && <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden />}
      {children}
    </span>
  )
}

/* ------------------------------------------------------------- data bits -- */

/** Every rupee figure. Mono and tabular so columns of them line up, and
 *  right-aligned wherever they sit in a table. */
export function Money({ paise, className = '' }: { paise: number; className?: string }) {
  return (
    <span className={`tnum font-mono ${className}`}>
      ₹{(paise / 100).toLocaleString('en-IN', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })}
    </span>
  )
}

export function Pct({ bps, className = '' }: { bps: number; className?: string }) {
  return (
    <span className={`tnum font-mono ${className}`}>
      {(bps / 100).toFixed(bps % 100 === 0 ? 0 : 2)}%
    </span>
  )
}

/**
 * The Z-Matrix metric card: eyebrow, a big tabular-mono figure, and a short
 * rule under it in the metric's own colour.
 *
 * The figure moved from the serif display face to JetBrains Mono, which is the
 * system's one hard rule -- every number in a data context is mono, so that
 * two stats side by side have digits of identical width and the eye can
 * compare them without re-reading. The rule is what makes a wall of these
 * scannable: colour appears at the bottom edge of each card, in a consistent
 * place, instead of being carried only by the digits.
 */
export function Stat({
  label,
  value,
  sub,
  tone = '',
}: {
  label: ReactNode
  value: ReactNode
  sub?: ReactNode
  /** A text-* class, e.g. "text-warn". Colours the figure and its rule. */
  tone?: string
}) {
  return (
    <div className="rounded-xl border border-hairline bg-card px-5 py-4 shadow-card">
      <Eyebrow>{label}</Eyebrow>
      <p
        className={[
          'tnum mt-2.5 font-mono text-[28px] font-semibold leading-none tracking-[-0.02em]',
          tone || 'text-ink',
        ].join(' ')}
      >
        {value}
      </p>
      <span
        aria-hidden
        className={[
          'mt-3 block h-[2px] w-9 rounded-full',
          tone ? `${tone} bg-current` : 'bg-copper',
        ].join(' ')}
      />
      {sub && <p className="mt-2.5 text-2xs text-ink-soft">{sub}</p>}
    </div>
  )
}

export function EmptyState({
  title,
  body,
  action,
}: {
  title: string
  body: string
  action?: ReactNode
}) {
  return (
    <div className="rounded-xl border border-dashed border-hairline-strong bg-card px-6 py-9 text-center">
      <p className="font-display text-[15px] font-bold tracking-[-0.01em] text-ink">{title}</p>
      <p className="mx-auto mt-2 max-w-md text-mini leading-relaxed text-ink-soft">
        {body}
      </p>
      {action && <div className="mt-5 flex justify-center">{action}</div>}
    </div>
  )
}

export function Callout({
  tone = 'neutral',
  children,
}: {
  tone?: Tone
  children: ReactNode
}) {
  const styles: Record<Tone, string> = {
    pass: 'border-pass-line bg-pass-bg text-ink',
    fail: 'border-fail-line bg-fail-bg text-ink',
    warn: 'border-warn-line bg-warn-bg text-ink',
    neutral: 'border-hairline bg-sunk text-ink-2',
    accent: 'border-accent-line bg-accent-soft text-ink',
  }
  return (
    <div className={`rounded-xl border px-5 py-4 text-mini leading-relaxed ${styles[tone]}`}>
      {children}
    </div>
  )
}

/* ---------------------------------------------------------------- tables -- */

export function TableWrap({ children }: { children: ReactNode }) {
  // Wide content scrolls inside its own container so the page body never
  // scrolls sideways.
  return (
    <div className="overflow-x-auto rounded-xl border border-hairline bg-card shadow-card">
      {children}
    </div>
  )
}

export const thClass =
  'px-4 py-2.5 text-left text-2xs font-semibold uppercase tracking-[0.12em] ' +
  'text-ink-soft whitespace-nowrap border-b border-hairline bg-surface'

export const tdClass = 'px-4 py-3 text-mini align-middle'

/** Cost and margin are the shop's private business. The tint plus the lock in
 *  the header is the visual answer to "mark it shop-only" -- it stops someone
 *  screenshotting this page onto a projector without noticing. */
export const sensitiveClass = 'bg-surface/70 text-ink-soft'
