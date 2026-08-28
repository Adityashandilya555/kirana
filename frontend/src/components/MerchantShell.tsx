import type { ReactNode } from 'react'
import { NavLink } from 'react-router-dom'

/**
 * The console frame: a dark aubergine rail against the mint canvas.
 *
 * A dark rail rather than a light one — high contrast at the edge, calm in the
 * middle, and it stops the console reading as another page of the customer app.
 * Desktop-first on purpose: a shopkeeper does this sitting down with a laptop,
 * not standing at a counter with one thumb. The rail still collapses to a
 * horizontal scroller under `md` so it is usable on a tablet.
 */

const LINKS: { to: string; label: string; hint: string }[] = [
  { to: '/merchant', label: 'Campaigns', hint: 'Running and past promotions' },
  { to: '/merchant/catalog', label: 'Products', hint: 'Prices, costs and margins' },
  { to: '/merchant/shelves', label: 'Shelves', hint: 'Group products for stickers' },
  { to: '/merchant/new', label: 'New campaign', hint: 'Plan, preview, commit' },
]

function Mark() {
  // Three bars at descending opacity: the ceilings a campaign commits to.
  return (
    <span
      aria-hidden
      className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-accent"
    >
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
        <rect x="2" y="3" width="12" height="2.4" rx="1.2" fill="#2d2142" />
        <rect x="2" y="7" width="9" height="2.4" rx="1.2" fill="#2d2142" opacity="0.7" />
        <rect x="2" y="11" width="6" height="2.4" rx="1.2" fill="#2d2142" opacity="0.4" />
      </svg>
    </span>
  )
}

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
    <div className="min-h-dvh bg-surface text-ink">
      <div className="mx-auto flex max-w-[1400px] flex-col md:flex-row">
        <aside className="shrink-0 bg-ink md:min-h-dvh md:w-[232px]">
          <div className="flex items-center gap-3 px-6 py-6">
            <Mark />
            <div className="min-w-0">
              <p className="truncate text-half font-semibold leading-tight text-sidebar-text">
                Kirana Agent
              </p>
              <p className="text-xxs text-sidebar-muted">Shopkeeper console</p>
            </div>
          </div>

          <div className="mx-4 border-t border-sidebar-line" />

          <nav className="flex gap-1 overflow-x-auto px-3 py-4 md:flex-col md:overflow-visible">
            {LINKS.map((l) => (
              <NavLink
                key={l.to}
                to={l.to}
                end={l.to === '/merchant'}
                className={({ isActive }) =>
                  [
                    'block whitespace-nowrap rounded-lg border px-3 py-2 transition-colors',
                    isActive
                      ? 'border-sidebar-line bg-sidebar-active'
                      : 'border-transparent hover:bg-sidebar-hover',
                  ].join(' ')
                }
              >
                {({ isActive }) => (
                  <>
                    <span
                      className={`block text-half font-medium ${
                        isActive ? 'text-sidebar-text' : 'text-sidebar-muted'
                      }`}
                    >
                      {l.label}
                    </span>
                    <span className="hidden text-xxs text-sidebar-muted md:block">
                      {l.hint}
                    </span>
                  </>
                )}
              </NavLink>
            ))}
          </nav>

          <div className="mx-4 hidden border-t border-sidebar-line md:block" />
          <div className="hidden px-6 py-4 md:block">
            <a
              href="/verify"
              className="text-tiny text-sidebar-muted underline-offset-2 hover:text-accent hover:underline"
            >
              Verify a redemption →
            </a>
          </div>
        </aside>

        <main className="min-w-0 flex-1 px-5 py-6 md:px-10 md:py-9">
          <header className="mb-8 flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0">
              <h1 className="font-display text-[32px] font-medium leading-[1.1] tracking-tight">
                {title}
              </h1>
              {subtitle && (
                <p className="mt-1.5 max-w-2xl text-half text-ink-soft">{subtitle}</p>
              )}
            </div>
            {actions && <div className="flex shrink-0 flex-wrap gap-2">{actions}</div>}
          </header>
          {children}
        </main>
      </div>
    </div>
  )
}
