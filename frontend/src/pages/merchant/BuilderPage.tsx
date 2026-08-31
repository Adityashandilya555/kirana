import { useCallback, useEffect, useMemo, useState } from 'react'
import MerchantShell from '../../components/MerchantShell'
import { Button, Callout, Card, Eyebrow, Money, Pct, Pill, inputClass } from '../../components/ui'
import {
  deleteShelf,
  getCatalog,
  getShelves,
  saveItems,
  saveShelf,
  simulate,
} from '../../lib/merchant'
import type { CatalogItem, Shelf, Simulation } from '../../lib/merchant'

/**
 * Build shelves by dragging products onto them.
 *
 * The screen this replaces cost 38 clicks across two pages to bind three
 * shelves of eight products: click-to-toggle tile grids, written twice --
 * once on ShelvesPage and again on NewCampaignPage -- with independently
 * implemented Set logic and no view that showed products and shelves at the
 * same time.
 *
 * Three things it adds beyond fewer clicks, and each is the reason a
 * shopkeeper would open it rather than the old page:
 *
 *   - Every product carries the discount its OWN margin allows, worked out by
 *     the same simulator the campaign preview uses, so you can see before you
 *     drop that sugar can never be discounted and tea can go to 19%.
 *   - Both bands are shown side by side, so "regulars get more" stops being an
 *     abstract setting and becomes two numbers on a card.
 *   - Products can be added and priced here. Until now the only way a product
 *     could enter the system was a spreadsheet import; `saveItems` existed in
 *     the API client and was imported by nothing.
 *
 * No drag library. This repo has five dependencies and inlines its own icons
 * rather than adding a sixth, so this is HTML5 drag events -- which are also
 * the only ones that work without a pointer-events polyfill on the tablet a
 * shopkeeper is likely holding.
 */

/** What a drag carries. A sku is enough; everything else is looked up. */
const DRAG_TYPE = 'application/x-kirana-sku'

interface CapRow {
  cap_bps: number
  discountable: boolean
  binding: string
  margin_at_list_pct: number
}

function ProductCard({
  item,
  cap,
  newBandBps,
  onDragStart,
  onEdit,
}: {
  item: CatalogItem
  cap: CapRow | undefined
  newBandBps: number | null
  onDragStart: (sku: string) => void
  onEdit: () => void
}) {
  const blocked = cap && !cap.discountable
  return (
    <div
      draggable
      onDragStart={(e) => {
        e.dataTransfer.setData(DRAG_TYPE, item.sku)
        e.dataTransfer.effectAllowed = 'copy'
        onDragStart(item.sku)
      }}
      className={[
        'group cursor-grab rounded-xl border bg-card px-3 py-2.5 shadow-card',
        'transition-colors duration-200 active:cursor-grabbing',
        blocked
          ? 'border-fail-line bg-fail-bg/40'
          : 'border-hairline hover:border-accent-line hover:bg-sunk',
      ].join(' ')}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-mini font-semibold text-ink">{item.name}</p>
          <p className="font-mono text-2xs text-ink-faint">{item.sku}</p>
        </div>
        <button
          type="button"
          onClick={onEdit}
          className="shrink-0 text-2xs text-ink-soft opacity-0 transition-opacity hover:text-accent group-hover:opacity-100"
        >
          edit
        </button>
      </div>

      <div className="mt-2 flex items-center justify-between gap-2">
        <Money paise={item.price_paise} className="text-2xs text-ink-soft" />
        {blocked ? (
          <Pill tone="fail">can't discount</Pill>
        ) : cap ? (
          <span className="flex items-baseline gap-1.5 text-2xs">
            {/* Both bands, side by side. The point of the whole tier feature
                made concrete: this is what a regular gets, and this is what a
                stranger gets. */}
            <span className="tabular-nums font-mono text-pass">
              <Pct bps={cap.cap_bps} />
            </span>
            {newBandBps !== null && (
              <span className="tabular-nums font-mono text-ink-faint">
                / <Pct bps={Math.floor((cap.cap_bps * newBandBps) / 10000)} />
              </span>
            )}
          </span>
        ) : null}
      </div>
    </div>
  )
}

export default function BuilderPage() {
  const [catalog, setCatalog] = useState<CatalogItem[]>([])
  const [shelves, setShelves] = useState<Shelf[]>([])
  const [sim, setSim] = useState<Simulation | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // The two numbers that decide every cap on screen. Defaults match the
  // campaign form so what you see here is what you will get there.
  const [maxDiscount, setMaxDiscount] = useState('20')
  const [marginFloor, setMarginFloor] = useState('12')
  const [newBandPct, setNewBandPct] = useState('50')

  const [dragging, setDragging] = useState<string | null>(null)
  const [over, setOver] = useState<string | null>(null)
  const [stickers, setStickers] = useState('24')

  const [editing, setEditing] = useState<Partial<CatalogItem> | null>(null)
  const [newShelfName, setNewShelfName] = useState('')

  /** Re-read both lists after any mutation. Returns the promise so callers
   *  can await it before clearing their busy flag. */
  const load = useCallback(
    () =>
      Promise.all([getCatalog(), getShelves()]).then(([c, s]) => {
        setCatalog(c.items)
        setShelves(s.shelves)
      }),
    [],
  )

  // Written as a plain promise chain rather than an awaited call, matching the
  // other merchant screens: the lint rule traces setState through a useCallback
  // and flags it, even though fetching on mount is exactly the "synchronising
  // with an external system" case the rule exists to allow.
  useEffect(() => {
    let alive = true
    Promise.all([getCatalog(), getShelves()])
      .then(([c, s]) => {
        if (!alive) return
        setCatalog(c.items)
        setShelves(s.shelves)
      })
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)))
    return () => {
      alive = false
    }
  }, [])

  const maxBps = Math.round(parseFloat(maxDiscount || '0') * 100)
  const floorBps = Math.round(parseFloat(marginFloor || '0') * 100)
  const newBandBps = Math.round(parseFloat(newBandPct || '0') * 100)

  // Same endpoint the campaign preview uses, so the caps shown here and the
  // caps committed there cannot disagree.
  useEffect(() => {
    if (!catalog.length || !maxBps) return
    const t = setTimeout(() => {
      simulate({
        max_discount_bps: maxBps,
        margin_floor_bps: floorBps,
        budget_paise: 500000,
        slot_count: Math.max(1, parseInt(stickers || '1', 10)),
      })
        .then(setSim)
        .catch(() => setSim(null))
    }, 350)
    return () => clearTimeout(t)
  }, [catalog.length, maxBps, floorBps, stickers])

  const caps = useMemo(() => {
    const m = new Map<string, CapRow>()
    for (const i of sim?.items ?? []) {
      m.set(i.sku, {
        cap_bps: i.max_discount_bps,
        discountable: i.discountable,
        binding: i.binding,
        margin_at_list_pct: i.margin_at_list_pct,
      })
    }
    return m
  }, [sim])

  async function dropOnShelf(shelf: Shelf, sku: string) {
    if (shelf.skus.includes(sku)) return
    setBusy(true)
    setError(null)
    try {
      await saveShelf({
        name: shelf.name,
        note: shelf.note,
        skus: [...shelf.skus, sku],
        shelf_id: shelf.id,
      })
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function removeFromShelf(shelf: Shelf, sku: string) {
    setBusy(true)
    try {
      // A shelf with nothing on it is not a shelf. The API rejects an empty
      // sku list, so the last removal deletes it rather than failing.
      if (shelf.skus.length <= 1) {
        await deleteShelf(shelf.id)
      } else {
        await saveShelf({
          name: shelf.name, note: shelf.note,
          skus: shelf.skus.filter((s) => s !== sku), shelf_id: shelf.id,
        })
      }
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  /** Make the shelf FIRST, empty, then fill it by dropping.
   *
   *  The first version created a shelf as a side effect of the first drop,
   *  which read as "the drop did something odd" rather than "I made a shelf".
   *  An empty shelf is fine: upsert_shelf accepts an empty sku list, and the
   *  old screen's refusal to save one was a frontend guard, not a rule. */
  async function createShelf() {
    const name = newShelfName.trim()
    if (!name) return
    setBusy(true)
    setError(null)
    try {
      await saveShelf({ name, note: '', skus: [], shelf_id: null })
      setNewShelfName('')
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  /** What catalog_items will actually accept, checked here so the shopkeeper
   *  gets a sentence instead of a constraint name from Postgres.
   *  price >= 100 paise and cost < price are CHECK constraints on the table;
   *  submitting a blank form sends 0 for both and trips the first one. */
  function productProblem(p: Partial<CatalogItem>): string | null {
    if (!p.sku?.trim()) return 'Give the product a short code, like TEA250.'
    if (!p.name?.trim()) return 'Give the product a name.'
    if (!p.price_paise || p.price_paise < 100) return 'Price must be at least ₹1.'
    if (p.cost_paise == null || p.cost_paise < 0) return 'Enter what this costs you.'
    if (p.cost_paise >= p.price_paise)
      return 'Cost has to be less than the price, or there is nothing to discount.'
    return null
  }

  async function saveProduct() {
    if (!editing) return
    const problem = productProblem(editing)
    if (problem) {
      setError(problem)
      return
    }
    setBusy(true)
    setError(null)
    try {
      await saveItems([
        {
          // productProblem() has already refused a blank code or name, so both
          // are present by the time we get here.
          sku: (editing.sku ?? '').trim().toUpperCase(),
          name: (editing.name ?? '').trim(),
          unit: editing.unit || 'pc',
          price_paise: editing.price_paise ?? 0,
          cost_paise: editing.cost_paise ?? 0,
        },
      ])
      setEditing(null)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const slotCount = Math.max(1, parseInt(stickers || '1', 10))
  const perShelf = shelves.length ? Math.floor(slotCount / shelves.length) : 0
  const spare = shelves.length ? slotCount % shelves.length : 0

  function dropHandlers(key: string, onDrop: (sku: string) => void) {
    return {
      onDragOver: (e: React.DragEvent) => {
        // preventDefault is what makes an element a drop target at all.
        if (e.dataTransfer.types.includes(DRAG_TYPE)) {
          e.preventDefault()
          setOver(key)
        }
      },
      onDragLeave: () => setOver((k) => (k === key ? null : k)),
      onDrop: (e: React.DragEvent) => {
        e.preventDefault()
        setOver(null)
        setDragging(null)
        const sku = e.dataTransfer.getData(DRAG_TYPE)
        if (sku) onDrop(sku)
      },
    }
  }

  return (
    <MerchantShell
      eyebrow="Workspace"
      title="Build your shelves"
      subtitle="Drag a product onto a shelf. Each card shows the most it can be discounted before your margin floor stops it."
      actions={
        <Button
          variant="ghost"
          onClick={() => setEditing({ sku: '', name: '', unit: 'pc' })}
        >
          Add a product
        </Button>
      }
    >
      {error && (
        <Callout tone="fail">
          <p className="font-semibold text-fail">{error}</p>
        </Callout>
      )}

      {/* ----------------------------------------------- product editor -- */}
      {/* Directly under the button that opens it. It used to render at the
          bottom of the page, below the product list and the shelves, so
          pressing "Add a product" scrolled nothing into view and read as a
          dead button. */}
      {editing && (
        <Card className="mb-5 border-accent-line bg-accent-soft/40">
          <Eyebrow>
            {catalog.some((c) => c.sku === editing.sku) ? 'Edit product' : 'New product'}
          </Eyebrow>
          <div className="mt-3 grid gap-3 sm:grid-cols-5">
            <label className="block">
              <span className="mb-1.5 block text-tiny font-semibold text-ink">Code</span>
              <input
                className={`${inputClass} font-mono uppercase`}
                placeholder="TEA250"
                value={editing.sku ?? ''}
                onChange={(e) => setEditing({ ...editing, sku: e.target.value })}
              />
            </label>
            <label className="block sm:col-span-2">
              <span className="mb-1.5 block text-tiny font-semibold text-ink">Name</span>
              <input
                className={inputClass}
                placeholder="Tata Tea Gold 250g"
                value={editing.name ?? ''}
                onChange={(e) => setEditing({ ...editing, name: e.target.value })}
              />
            </label>
            <label className="block">
              <span className="mb-1.5 block text-tiny font-semibold text-ink">Price (₹)</span>
              <input
                className={inputClass}
                inputMode="decimal"
                placeholder="190"
                value={editing.price_paise ? String(editing.price_paise / 100) : ''}
                onChange={(e) =>
                  setEditing({
                    ...editing,
                    price_paise: Math.round(parseFloat(e.target.value || '0') * 100),
                  })
                }
              />
            </label>
            <label className="block">
              <span className="mb-1.5 block text-tiny font-semibold text-ink">
                Cost (₹) 🔒
              </span>
              <input
                className={inputClass}
                inputMode="decimal"
                placeholder="120"
                value={editing.cost_paise ? String(editing.cost_paise / 100) : ''}
                onChange={(e) =>
                  setEditing({
                    ...editing,
                    cost_paise: Math.round(parseFloat(e.target.value || '0') * 100),
                  })
                }
              />
            </label>
          </div>
          <p className="mt-2 text-tiny leading-relaxed text-ink-soft">
            Cost never leaves your shop — it is what works out the discount a
            product can carry, and the assistant is never told it.
          </p>
          <div className="mt-4 flex gap-2">
            <Button onClick={() => void saveProduct()} disabled={busy}>
              {busy ? 'Saving…' : 'Save product'}
            </Button>
            <Button variant="ghost" onClick={() => { setEditing(null); setError(null) }}>
              Cancel
            </Button>
          </div>
        </Card>
      )}

      {/* ------------------------------------------------------- limits -- */}
      <Card className="mb-5">
        <Eyebrow>The numbers these caps come from</Eyebrow>
        <div className="mt-3 grid gap-3 sm:grid-cols-4">
          <label className="block">
            <span className="mb-1.5 block text-tiny font-semibold text-ink">
              Maximum discount (%)
            </span>
            <input className={inputClass} inputMode="decimal" value={maxDiscount}
                   onChange={(e) => setMaxDiscount(e.target.value)} />
          </label>
          <label className="block">
            <span className="mb-1.5 block text-tiny font-semibold text-ink">
              Margin floor (%)
            </span>
            <input className={inputClass} inputMode="decimal" value={marginFloor}
                   onChange={(e) => setMarginFloor(e.target.value)} />
          </label>
          <label className="block">
            <span className="mb-1.5 block text-tiny font-semibold text-ink">
              New customers get (%)
            </span>
            <input className={inputClass} inputMode="numeric" value={newBandPct}
                   onChange={(e) => setNewBandPct(e.target.value)} />
          </label>
          <label className="block">
            <span className="mb-1.5 block text-tiny font-semibold text-ink">
              Stickers
            </span>
            <input className={inputClass} inputMode="numeric" value={stickers}
                   onChange={(e) => setStickers(e.target.value)} />
          </label>
        </div>
        <p className="mt-2.5 text-tiny leading-relaxed text-ink-soft">
          Green is what a regular can reach; grey is a first-time visitor. Worked
          out by the same simulator the campaign preview uses, so these are the
          numbers that will actually be committed.
        </p>
      </Card>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,340px)_minmax(0,1fr)]">
        {/* ---------------------------------------------------- products -- */}
        <div>
          <div className="mb-2 flex items-baseline justify-between">
            <Eyebrow>Your products</Eyebrow>
            <span className="text-2xs text-ink-soft">{catalog.length} items</span>
          </div>

          {catalog.length === 0 ? (
            <Card className="text-center">
              <p className="text-mini text-ink-soft">
                Nothing here yet. Import a sheet from Products, or add one by hand.
              </p>
            </Card>
          ) : (
            <div className="space-y-2">
              {catalog.map((c) => (
                <ProductCard
                  key={c.sku}
                  item={c}
                  cap={caps.get(c.sku)}
                  newBandBps={newBandBps}
                  onDragStart={setDragging}
                  onEdit={() => setEditing(c)}
                />
              ))}
            </div>
          )}
        </div>

        {/* ------------------------------------------------------ shelves -- */}
        <div>
          <div className="mb-2 flex items-baseline justify-between">
            <Eyebrow>Shelves</Eyebrow>
            {shelves.length > 0 && (
              <span className="text-2xs text-ink-soft">
                {slotCount} stickers ≈ {perShelf} each
                {spare > 0 && `, ${spare} shelf${spare > 1 ? 'ves' : ''} get one more`}
              </span>
            )}
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            {shelves.map((s) => (
              <Card
                key={s.id}
                className={[
                  'transition-colors duration-200',
                  over === s.id
                    ? 'border-accent bg-accent-soft'
                    : // While something is being dragged, every shelf shows it
                      // will accept it. Without this the only feedback is the
                      // cursor, and on a tablet there is no cursor.
                      dragging
                      ? 'border-accent-line border-dashed'
                      : '',
                ].join(' ')}
                {...dropHandlers(s.id, (sku) => void dropOnShelf(s, sku))}
              >
                <div className="flex items-baseline justify-between gap-2">
                  <p className="text-mini font-semibold text-ink">{s.name}</p>
                  {/* The round-robin split, shown rather than described. */}
                  <span className="font-mono text-2xs text-ink-soft">
                    {perShelf + (shelves.indexOf(s) < spare ? 1 : 0)} stickers
                  </span>
                </div>

                <div className="mt-2.5 flex flex-wrap gap-1.5">
                  {s.skus.map((sku) => (
                    <button
                      key={sku}
                      type="button"
                      onClick={() => void removeFromShelf(s, sku)}
                      title="Remove from this shelf"
                      className="group rounded-md bg-sunk px-2 py-0.5 font-mono text-2xs text-ink-2 transition-colors hover:bg-fail-bg hover:text-fail"
                    >
                      {sku}
                      <span className="ml-1 opacity-0 transition-opacity group-hover:opacity-100">
                        ×
                      </span>
                    </button>
                  ))}
                  {s.skus.length === 0 && (
                    <span className="text-2xs text-ink-faint">empty</span>
                  )}
                </div>

                <p className="mt-2.5 text-2xs text-ink-faint">
                  Drop a product here, or click a code to take it off.
                </p>
              </Card>
            ))}

            {/* ------------------------------------------------ new shelf -- */}
            {/* Name it, make it, then fill it. Creating a shelf as a side
                effect of the first drop read as the drop misbehaving. */}
            <form
              onSubmit={(e) => {
                e.preventDefault()
                void createShelf()
              }}
              className="flex min-h-[128px] flex-col justify-center gap-2 rounded-xl border border-dashed border-hairline-strong bg-card px-4 py-5"
            >
              <label
                htmlFor="new-shelf"
                className="text-tiny font-semibold text-ink"
              >
                New shelf
              </label>
              <input
                id="new-shelf"
                className={inputClass}
                placeholder="Tea &amp; coffee"
                value={newShelfName}
                onChange={(e) => setNewShelfName(e.target.value)}
              />
              <Button type="submit" disabled={busy || !newShelfName.trim()} full>
                Create shelf
              </Button>
              <p className="text-2xs leading-relaxed text-ink-faint">
                It starts empty. Drag products onto it afterwards.
              </p>
            </form>
          </div>

          {shelves.length === 0 && catalog.length > 0 && (
            <Callout tone="neutral">
              Without shelves, every sticker covers your whole shop. Group the
              products you want a sticker to be about, and the assistant will
              never learn the others exist.
            </Callout>
          )}
        </div>
      </div>

    </MerchantShell>
  )
}
