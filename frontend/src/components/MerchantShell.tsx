import type { ReactNode } from 'react'
import { NavLink } from 'react-router-dom'

/**
 * The console frame: a Deep Obsidian rail against the Peach Skyline canvas.
 *
 * Rebuilt on the Z-Matrix rail (Matrix-bd `preview/components-sidebar.html`).
 * The old rail was a stack of two-line links -- label above a sentence of hint
 * -- which made every row 44px tall, gave the eye no anchor to scan down, and
 * left the active item marked only by a slightly lighter fill. Four items
 * filled the whole rail.
 *
 * What Z-Matrix does instead, and what this now does:
 *
 *   - one line per item, icon + label, so the icons form a scannable column;
 *   - a 2px accent bar welded to the left edge of the active row, which is
 *     the thing that actually reads as "you are here" at a glance;
 *   - the icon takes the accent colour when active, so the cue survives even
 *     when the fill is subtle;
 *   - uppercase section headers at 0.14em tracking, which turn a flat list
 *     into groups without drawing a single line;
 *   - counts right-aligned in mono, because a number in a nav is data.
 *
 * The hints did not deserve a line each, but they were genuinely useful, so
 * they moved to `title` -- still there on hover, no longer costing vertical
 * rhythm.
 *
 * Desktop-first on purpose: a shopkeeper does this sitting down with a laptop,
 * not standing at a counter with one thumb. The rail still collapses to a
 * horizontal scroller under `md` so it is usable on a tablet.
 */

/* -------------------------------------------------------------- icons -- */
/* Lucide geometry, 1.6px stroke, currentColor -- Z-Matrix allows no emoji in
   chrome and no filled icons. Inlined rather than pulling in a dependency for
   five glyphs. */

type IconProps = { className?: string }

const ico = 'h-4 w-4 shrink-0'

function IconCampaigns({ className = ico }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="3" y="3" width="7" height="7" rx="1.5" />
      <rect x="14" y="3" width="7" height="7" rx="1.5" />
      <rect x="3" y="14" width="7" height="7" rx="1.5" />
      <rect x="14" y="14" width="7" height="7" rx="1.5" />
    </svg>
  )
}

function IconProducts({ className = ico }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M21 8v8a2 2 0 0 1-1 1.73l-7 4a2 2 0 0 1-2 0l-7-4A2 2 0 0 1 3 16V8a2 2 0 0 1 1-1.73l7-4a2 2 0 0 1 2 0l7 4A2 2 0 0 1 21 8z" />
      <path d="m3.3 7 8.7 5 8.7-5" />
      <path d="M12 22V12" />
    </svg>
  )
}

function IconShelves({ className = ico }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="m12 2 9 5-9 5-9-5 9-5z" />
      <path d="m3 12 9 5 9-5" />
      <path d="m3 17 9 5 9-5" />
    </svg>
  )
}

function IconNew({ className = ico }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <path d="M14 2v6h6" />
      <path d="M12 12v6" />
      <path d="M9 15h6" />
    </svg>
  )
}

function IconAgent({ className = ico }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="4" y="8" width="16" height="12" rx="2" />
      <path d="M12 4v4" />
      <circle cx="12" cy="3" r="1" />
      <path d="M9 13h.01M15 13h.01" />
      <path d="M9.5 17h5" />
    </svg>
  )
}

function IconVerify({ className = ico }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M12 2 20 6v6c0 5-3.4 9.4-8 10-4.6-.6-8-5-8-10V6z" />
      <path d="m9 12 2 2 4-4" />
    </svg>
  )
}

/* --------------------------------------------------------------- mark -- */

function Mark() {
  // Three bars at descending opacity: the ceilings a campaign commits to.
  // On the obsidian rail the mark is drawn in the rail accent on a tinted
  // well, not a solid accent tile -- a bright block that size fights the
  // wordmark next to it.
  return (
    <span
      aria-hidden
      className="grid h-8 w-8 shrink-0 place-items-center rounded-lg border border-rail-accent/25 bg-rail-accent/12"
    >
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
        <rect x="2" y="3" width="12" height="2.4" rx="1.2" fill="currentColor" className="text-rail-accent" />
        <rect x="2" y="7" width="9" height="2.4" rx="1.2" fill="currentColor" className="text-rail-accent" opacity="0.7" />
        <rect x="2" y="11" width="6" height="2.4" rx="1.2" fill="currentColor" className="text-rail-accent" opacity="0.4" />
      </svg>
    </span>
  )
}

/* ---------------------------------------------------------------- nav -- */

type Item = {
  to: string
  label: string
  hint: string
  icon: (p: IconProps) => ReactNode
  end?: boolean
  key?: CountKey
}

/** Optional counts, so the rail can carry data the way Z-Matrix's does. */
export type CountKey = 'campaigns' | 'products' | 'shelves'

const SECTIONS: { heading: string; items: Item[] }[] = [
  {
    heading: 'Workspace',
    items: [
      { to: '/merchant', label: 'Campaigns', hint: 'Running and past promotions', icon: IconCampaigns, end: true, key: 'campaigns' },
      { to: '/merchant/catalog', label: 'Products', hint: 'Prices, costs and margins', icon: IconProducts, key: 'products' },
      { to: '/merchant/shelves', label: 'Shelves', hint: 'Group products for stickers', icon: IconShelves, key: 'shelves' },
      { to: '/merchant/agent', label: 'AI shoppers', hint: "Let other people's assistants buy from you", icon: IconAgent },
    ],
  },
  {
    heading: 'Create',
    items: [
      { to: '/merchant/new', label: 'New campaign', hint: 'Plan, preview, commit', icon: IconNew },
    ],
  },
]

function RailLink({ item, count }: { item: Item; count?: number }) {
  const Icon = item.icon
  return (
    <NavLink
      to={item.to}
      end={item.end}
      title={item.hint}
      className={({ isActive }) =>
        [
          'group relative flex items-center gap-2.5 rounded-lg px-2.5 py-2',
          'text-mini transition-colors duration-150 ease-[cubic-bezier(0.2,0.7,0.2,1)]',
          'whitespace-nowrap',
          isActive
            ? 'bg-rail-active font-semibold text-rail-text'
            : 'font-medium text-rail-muted hover:bg-rail-hover hover:text-rail-text',
        ].join(' ')
      }
    >
      {({ isActive }) => (
        <>
          {/* The "you are here" cue. Welded to the left edge, inset top and
              bottom so it reads as a marker rather than a border. */}
          <span
            aria-hidden
            className={[
              'absolute inset-y-1.5 left-0 w-[2px] rounded-full transition-opacity duration-150',
              isActive ? 'bg-rail-accent opacity-100' : 'opacity-0',
            ].join(' ')}
          />
          <Icon
            className={[
              ico,
              'transition-colors duration-150',
              isActive ? 'text-rail-accent' : 'text-rail-faint group-hover:text-rail-muted',
            ].join(' ')}
          />
          <span className="min-w-0 truncate">{item.label}</span>
          {typeof count === 'number' && (
            <span
              className={[
                'tnum ml-auto pl-2 font-mono text-2xs',
                isActive ? 'text-rail-accent' : 'text-rail-faint',
              ].join(' ')}
            >
              {count}
            </span>
          )}
        </>
      )}
    </NavLink>
  )
}

/* -------------------------------------------------------------- shell -- */

export default function MerchantShell({
  title,
  subtitle,
  eyebrow,
  actions,
  counts,
  children,
}: {
  title: string
  subtitle?: string
  /** Small uppercase label above the page title. Reads as a breadcrumb, so it
   *  names the rail section this page sits in -- repeating the product name
   *  here would just restate the wordmark two inches to the left. */
  eyebrow?: string
  actions?: ReactNode
  /** Optional per-section counts shown right-aligned in the rail. */
  counts?: Partial<Record<CountKey, number>>
  children: ReactNode
}) {
  return (
    <div className="min-h-dvh bg-surface text-ink">
      <div className="mx-auto flex max-w-[1440px] flex-col md:flex-row">
        {/* ------------------------------------------------------- rail -- */}
        {/* No blueprint grid on the rail: it is a solid plane, and lines on it
            read as empty columns under the nav rather than as texture. */}
        <aside className="zm-rail shrink-0 border-b border-rail-line bg-rail md:min-h-dvh md:w-[248px] md:border-b-0 md:border-r">
          <div className="md:sticky md:top-0">
            <div className="flex items-center gap-2.5 px-4 py-5">
              <Mark />
              <div className="min-w-0">
                <p className="truncate font-display text-mini font-extrabold uppercase leading-tight tracking-[0.1em] text-rail-text">
                  Kirana
                  <span className="ml-1.5 font-normal tracking-[0.3em] text-rail-muted">
                    AGENT
                  </span>
                </p>
                <p className="mt-0.5 text-2xs text-rail-faint">Shopkeeper console</p>
              </div>
            </div>

            <div className="mx-4 border-t border-rail-line" />

            {/* Under md this is one horizontal scroller; the section headings
                would only add noise there, so they are desktop-only. */}
            <nav className="flex gap-1 overflow-x-auto px-3 py-3 md:flex-col md:gap-0.5 md:overflow-visible md:py-4">
              {SECTIONS.map((section) => (
                <div key={section.heading} className="contents md:block">
                  <p className="hidden px-2.5 pb-1.5 pt-3 text-2xs font-semibold uppercase tracking-[0.14em] text-rail-section first:pt-0 md:block">
                    {section.heading}
                  </p>
                  {section.items.map((item) => (
                    <RailLink
                      key={item.to}
                      item={item}
                      count={item.key ? counts?.[item.key] : undefined}
                    />
                  ))}
                </div>
              ))}
            </nav>

            <div className="mx-4 hidden border-t border-rail-line md:block" />

            <div className="hidden px-3 py-4 md:block">
              <p className="px-2.5 pb-1.5 text-2xs font-semibold uppercase tracking-[0.14em] text-rail-section">
                Counter
              </p>
              <a
                href="/verify"
                className="group flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-mini font-medium text-rail-muted transition-colors duration-150 hover:bg-rail-hover hover:text-rail-text"
              >
                <IconVerify className={`${ico} text-rail-faint group-hover:text-rail-muted`} />
                <span>Verify a redemption</span>
              </a>
            </div>
          </div>
        </aside>

        {/* ------------------------------------------------------- main -- */}
        {/* The blueprint grid lives on the page ground and never on a card,
            so the white surfaces float above it instead of sitting on a wash. */}
        <main className="zm-grid min-w-0 flex-1 px-5 py-6 md:px-10 md:py-9">
          <header className="mb-8 flex flex-wrap items-end justify-between gap-4 border-b border-hairline pb-5">
            <div className="min-w-0">
              <p className="text-2xs font-semibold uppercase tracking-[0.14em] text-ink-soft">
                {eyebrow ?? 'Workspace'}
              </p>
              <h1 className="mt-1.5 font-display text-[30px] font-bold leading-[1.1] tracking-[-0.02em] text-ink">
                {title}
              </h1>
              {subtitle && (
                <p className="mt-2 max-w-2xl text-half leading-relaxed text-ink-soft">
                  {subtitle}
                </p>
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
