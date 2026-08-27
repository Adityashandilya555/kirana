import type { ReactNode } from 'react'
import { NavLink } from 'react-router-dom'

/**
 * The console frame. Desktop-first: a shopkeeper does this sitting down with a
 * laptop, not standing at a counter with one thumb.
 *
 * The nav is a left rail rather than a top bar because the two data-heavy
 * screens (catalog and audit) are tables that want vertical room more than
 * horizontal, and a rail costs 200px of width but no height.
 */

const LINKS: { to: string; label: string; hint: string }[] = [
  { to: '/merchant', label: 'Campaigns', hint: 'Running and past promotions' },
  { to: '/merchant/catalog', label: 'Products', hint: 'Prices, costs and margins' },
  { to: '/merchant/shelves', label: 'Shelves', hint: 'Group products for stickers' },
  { to: '/merchant/new', label: 'New campaign', hint: 'Plan, preview, commit' },
]

export default function MerchantShell({
  title,
  subtitle,
  actions,
  children,
}: {
  title: string
  subtitle?: string
  actions?: ReactNode
  children: ReactNode
}) {
  return (
    <div className="min-h-dvh bg-canvas text-ink">
      <div className="mx-auto flex max-w-[1400px] flex-col md:flex-row">
        <aside className="shrink-0 border-b border-hairline bg-surface md:min-h-dvh md:w-[212px] md:border-b-0 md:border-r">
          <div className="px-5 py-5">
            <p className="text-sm font-semibold tracking-tight">Kirana Agent</p>
            <p className="mt-0.5 text-xs text-ink-soft">Shopkeeper console</p>
          </div>
          <nav className="flex gap-1 overflow-x-auto px-3 pb-3 md:flex-col md:overflow-visible md:pb-5">
            {LINKS.map((l) => (
              <NavLink
                key={l.to}
                to={l.to}
                end={l.to === '/merchant'}
                className={({ isActive }) =>
                  [
                    'block whitespace-nowrap rounded-lg px-3 py-2 text-sm transition-colors',
                    isActive
                      ? 'bg-accent-soft font-medium text-accent'
                      : 'text-ink-soft hover:bg-sunk hover:text-ink',
                  ].join(' ')
                }
              >
                <span className="block">{l.label}</span>
                <span className="hidden text-[11px] font-normal text-ink-soft md:block">
                  {l.hint}
                </span>
              </NavLink>
            ))}
          </nav>
          <div className="hidden border-t border-hairline px-5 py-4 md:block">
            <a
              href="/verify"
              className="text-xs text-ink-soft underline underline-offset-2 hover:text-accent"
            >
              Verify a redemption →
            </a>
          </div>
        </aside>

        <main className="min-w-0 flex-1 px-5 py-6 md:px-8 md:py-8">
          <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0">
              <h1 className="text-xl font-semibold tracking-tight md:text-2xl">{title}</h1>
              {subtitle && <p className="mt-1 text-sm text-ink-soft">{subtitle}</p>}
            </div>
            {actions && <div className="flex shrink-0 flex-wrap gap-2">{actions}</div>}
          </header>
          {children}
        </main>
      </div>
    </div>
  )
}

/* ---------------------------------------------------------------- atoms -- */

export function Card({
  children,
  className = '',
}: {
  children: ReactNode
  className?: string
}) {
  return (
    <section
      className={`rounded-xl border border-hairline bg-surface p-5 ${className}`}
    >
      {children}
    </section>
  )
}

export function Button({
  children,
  onClick,
  type = 'button',
  variant = 'primary',
  disabled,
  title,
}: {
  children: ReactNode
  onClick?: () => void
  type?: 'button' | 'submit'
  variant?: 'primary' | 'ghost' | 'danger'
  disabled?: boolean
  title?: string
}) {
  const styles = {
    primary: 'bg-accent text-white hover:opacity-90',
    ghost: 'border border-hairline bg-surface text-ink hover:bg-sunk',
    danger: 'border border-fail/40 bg-fail-soft text-fail hover:bg-fail/10',
  }[variant]
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`rounded-lg px-3.5 py-2 text-sm font-medium transition-colors disabled:opacity-40 ${styles}`}
    >
      {children}
    </button>
  )
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: ReactNode
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-ink-soft">{hint}</span>}
    </label>
  )
}

export const inputClass =
  'w-full rounded-lg border border-hairline bg-surface px-3 py-2 text-sm outline-none focus:border-accent'

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
    <Card className="text-center">
      <p className="font-medium">{title}</p>
      <p className="mx-auto mt-1 max-w-md text-sm text-ink-soft">{body}</p>
      {action && <div className="mt-4 flex justify-center">{action}</div>}
    </Card>
  )
}

export function Money({ paise, className = '' }: { paise: number; className?: string }) {
  return (
    <span className={`tnum ${className}`}>
      ₹{(paise / 100).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
    </span>
  )
}

export function Pct({ bps }: { bps: number }) {
  return <span className="tnum">{(bps / 100).toFixed(bps % 100 === 0 ? 0 : 2)}%</span>
}
