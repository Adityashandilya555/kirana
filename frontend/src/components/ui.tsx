import type { ReactNode } from 'react'

/**
 * The seven recipes every surface is built from.
 *
 * These exist because the design was ported from an export written entirely in
 * inline style objects -- 1,100 lines with zero className. Translating that
 * per-component would have produced seven slightly different cards. Naming the
 * recipes once means a change to the card shape is one edit, and it keeps the
 * geometry consistent across fourteen screens.
 *
 * Depth is inset-versus-elevated rather than stacked shadow: white cards on the
 * mint canvas carry one 5%-opacity shadow, and panels *inside* those cards go
 * darker (surface/sunk) rather than lighter. Two levels, no more.
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
        'rounded-2xl border border-hairline bg-card',
        'shadow-[0_1px_4px_rgba(45,33,66,0.05)]',
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
    <header className="flex flex-wrap items-start justify-between gap-3 border-b border-hairline px-6 py-5">
      <div className="min-w-0">
        <h2 className="font-display text-lg font-medium leading-tight">{title}</h2>
        {sub && <p className="mt-0.5 text-mini text-ink-soft">{sub}</p>}
      </div>
      {actions && <div className="flex shrink-0 gap-2">{actions}</div>}
    </header>
  )
}

/* --------------------------------------------------------------- eyebrow -- */

/** The most repeated typographic device in the design: a small uppercase
 *  label above a number or a section. */
export function Eyebrow({ children }: { children: ReactNode }) {
  return (
    <span className="block text-xxs font-semibold uppercase tracking-[0.06em] text-ink-soft">
      {children}
    </span>
  )
}

/* --------------------------------------------------------------- buttons -- */

type Variant = 'primary' | 'ghost' | 'danger' | 'accent'

const VARIANTS: Record<Variant, string> = {
  // Ink, not turquoise. #7FDBD3 behind white text is ~1.6:1 -- the accent is a
  // fill colour, and using it for a CTA would make the label unreadable.
  primary:
    'bg-ink text-white hover:bg-sidebar-hover shadow-[0_1px_3px_rgba(45,33,66,0.25)]',
  ghost: 'border border-hairline bg-card text-ink hover:bg-sunk',
  danger: 'border border-fail-line bg-fail-bg text-fail hover:bg-fail/10',
  // Turquoise fill with ink text: the one place the accent is a background for
  // type, and it works because the text is dark.
  accent: 'bg-accent text-ink hover:bg-accent-strong',
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
        'inline-flex items-center justify-center gap-2 rounded-[10px] px-4 py-2.5',
        'text-half font-semibold transition-colors duration-150',
        'disabled:cursor-default disabled:opacity-40',
        full ? 'w-full' : '',
        VARIANTS[variant],
      ].join(' ')}
    >
      {children}
    </button>
  )
}

/* ---------------------------------------------------------------- inputs -- */

/** 1.5px border and a surface background, so a field reads as a well cut into
 *  the white card rather than a box drawn on top of it. */
export const inputClass =
  'w-full rounded-[10px] border-[1.5px] border-hairline bg-surface px-3.5 py-2.5 ' +
  'text-half text-ink outline-none transition-colors placeholder:text-ink-soft/60 ' +
  'focus:border-accent-strong'

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
      <span className="mb-1.5 block text-half font-semibold text-ink">{label}</span>
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
  neutral: 'bg-sunk text-ink-soft border-hairline',
  accent: 'bg-accent-soft text-ink border-accent',
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

/** A hero figure: serif, not mono. Mono is for identifiers; Fraunces is for
 *  numbers meant to be read at a glance from across a room. */
export function Stat({
  label,
  value,
  sub,
  tone = '',
}: {
  label: ReactNode
  value: ReactNode
  sub?: ReactNode
  tone?: string
}) {
  return (
    <div className="rounded-xl border border-hairline bg-surface px-5 py-4">
      <Eyebrow>{label}</Eyebrow>
      <p className={`mt-2 font-display text-[28px] font-medium leading-none ${tone}`}>
        {value}
      </p>
      {sub && <p className="mt-1 text-xxs text-ink-soft">{sub}</p>}
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
    <div className="rounded-xl border-[1.5px] border-dashed border-hairline bg-card px-6 py-8 text-center">
      <p className="font-display text-lg font-medium">{title}</p>
      <p className="mx-auto mt-1.5 max-w-md text-mini leading-relaxed text-ink-soft">
        {body}
      </p>
      {action && <div className="mt-4 flex justify-center">{action}</div>}
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
    pass: 'border-pass-line bg-pass-bg',
    fail: 'border-fail-line bg-fail-bg',
    warn: 'border-warn-line bg-warn-bg',
    neutral: 'border-hairline bg-sunk',
    accent: 'border-accent bg-accent-soft',
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
    <div className="overflow-x-auto rounded-xl border border-hairline bg-card">
      {children}
    </div>
  )
}

export const thClass =
  'px-4 py-2.5 text-left text-xxs font-bold uppercase tracking-[0.06em] ' +
  'text-ink-soft whitespace-nowrap border-b border-hairline'

export const tdClass = 'px-4 py-3 text-mini align-middle'

/** Cost and margin are the shop's private business. The tint plus the lock in
 *  the header is the visual answer to "mark it shop-only" -- it stops someone
 *  screenshotting this page onto a projector without noticing. */
export const sensitiveClass = 'bg-surface/60 text-ink-soft'
