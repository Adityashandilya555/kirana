import { useCallback, useEffect, useRef, useState } from 'react'
import MerchantShell from '../../components/MerchantShell'
import { Button, Card, EmptyState, Money, Pct } from '../../components/ui'
import { API_BASE, MERCHANT_KEY } from '../../lib/api'
import { bandCapBps, bpsFromPct } from '../../lib/caps'
import { getCatalog, importSheet, previewSheet, simulate } from '../../lib/merchant'
import type { CatalogItem, ParsedSheet, SimItem } from '../../lib/merchant'

/**
 * Products, and getting them in from a spreadsheet.
 *
 * The import is two steps on purpose — preview, then confirm. A shopkeeper has
 * no undo, so showing exactly which rows will land and which are refused,
 * before anything is written, is the difference between a tool they trust and
 * one they are afraid of.
 *
 * Margin is computed and shown prominently because it is the number that
 * silently decides whether an item can be discounted at all, and it is not
 * something a price list usually spells out.
 */

function MarginCell({ bps }: { bps: number }) {
  const tone =
    bps >= 2000 ? 'text-pass' : bps >= 1300 ? 'text-ink' : 'text-warn'
  return (
    <span className={`tnum font-medium ${tone}`}>
      <Pct bps={bps} />
    </span>
  )
}

/**
 * The ceiling a product can carry, worked out from its own margin.
 *
 * Margin is already shown here, but margin is not the number a shopkeeper
 * actually wants: "36% margin" does not tell you what you may offer, and the
 * answer depends on the floor you set. This is that answer, and it is the same
 * one the campaign preview shows and the gate later enforces -- all three come
 * from simulate.item_headroom, so there is only one number in the system.
 *
 * Both bands, because the point of the tier rule is invisible until you see
 * that a regular reaches 19% on tea and a stranger reaches 9.5%.
 */
function CapCell({ capBps, discountable }: { capBps: number; discountable: boolean }) {
  if (!discountable) {
    // The one a shopkeeper most needs to see. A product whose margin sits
    // under the floor can never be discounted at all, and until this column
    // existed that was only discoverable by committing a campaign.
    return <span className="text-xxs font-semibold uppercase tracking-wide text-fail">none</span>
  }
  return (
    <span className="tnum font-mono text-mini text-pass">
      <Pct bps={capBps} />
    </span>
  )
}

export default function CatalogPage() {
  const [items, setItems] = useState<CatalogItem[] | null>(null)
  const [preview, setPreview] = useState<ParsedSheet | null>(null)
  const [pendingFile, setPendingFile] = useState<File | null>(null)
  const [replace, setReplace] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  // The three numbers that decide every ceiling below. Defaults match the
  // campaign form, so what a shopkeeper reads here is what they will get there.
  const [maxDiscount, setMaxDiscount] = useState('20')
  const [marginFloor, setMarginFloor] = useState('12')
  const [newBandPct, setNewBandPct] = useState('50')
  const [caps, setCaps] = useState<Map<string, SimItem>>(new Map())

  // Derived during render, not stored: a box holding "1" on the way to "12",
  // or holding nothing at all, is a normal thing for a box to hold, and the
  // answer is simply that there is no ceiling to show yet.
  const maxBps = bpsFromPct(maxDiscount)
  const floorBps = bpsFromPct(marginFloor)
  const bandBps = bpsFromPct(newBandPct) ?? 0
  const capsReady = maxBps !== null && floorBps !== null && maxBps > 0

  const load = useCallback(() => {
    getCatalog()
      .then((r) => setItems(r.items))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [])

  useEffect(load, [load])

  /*
   * The ceilings come from the same /simulate the campaign preview uses, not
   * from arithmetic repeated here. Three places now show a product's cap --
   * this page, the campaign preview, and the committed value the gate
   * enforces -- and all three trace back to simulate.item_headroom, so they
   * cannot drift into disagreeing about the same product.
   *
   * Debounced, because these are number inputs and a request per keystroke
   * would both hammer the API and make the column flicker mid-typing.
   */
  useEffect(() => {
    // Spelled out rather than leaning on capsReady: strictNullChecks is off in
    // this project, so nothing would stop a null reaching the request body.
    if (!items?.length || maxBps === null || floorBps === null || maxBps <= 0) return
    const t = setTimeout(() => {
      simulate({
        max_discount_bps: maxBps,
        margin_floor_bps: floorBps,
        // Neither affects a per-item ceiling; the endpoint just wants them.
        budget_paise: 500_000,
        slot_count: 24,
      })
        .then((s) => setCaps(new Map(s.items.map((i) => [i.sku, i]))))
        .catch(() => setCaps(new Map()))
    }, 350)
    return () => clearTimeout(t)
  }, [items, maxBps, floorBps])

  async function choose(file: File) {
    setError(null)
    setBusy(true)
    setPendingFile(file)
    try {
      setPreview(await previewSheet(file))
    } catch (e) {
      setPreview(null)
      setPendingFile(null)
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function confirmImport() {
    if (!pendingFile) return
    setBusy(true)
    setError(null)
    try {
      const done = await importSheet(pendingFile, replace)
      setPreview(null)
      setPendingFile(null)
      setReplace(false)
      if (inputRef.current) inputRef.current.value = ''
      load()
      setError(
        `Imported ${done.imported?.upserted ?? 0} product(s)` +
          (done.imported?.deactivated
            ? `, retired ${done.imported.deactivated} no longer in the sheet.`
            : '.'),
      )
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <MerchantShell
      title="Products"
      subtitle="What the assistant is allowed to sell, and what each item really costs you."
      actions={
        <>
          <a
            href={`${API_BASE}/api/v1/catalog/template.csv`}
            className="rounded-lg border border-hairline bg-surface px-3.5 py-2 text-sm font-medium hover:bg-sunk"
            onClick={(e) => {
              // The endpoint is key-protected, so a plain link would 401.
              e.preventDefault()
              fetch(`${API_BASE}/api/v1/catalog/template.csv`, {
                headers: { 'X-Merchant-Key': MERCHANT_KEY },
              })
                .then((r) => r.blob())
                .then((b) => {
                  const url = URL.createObjectURL(b)
                  const a = document.createElement('a')
                  a.href = url
                  a.download = 'kirana-catalog-template.csv'
                  a.click()
                  URL.revokeObjectURL(url)
                })
                .catch(() => setError('Could not download the template.'))
            }}
          >
            Download template
          </a>
          <Button onClick={() => inputRef.current?.click()}>Upload a sheet</Button>
        </>
      }
    >
      <input
        ref={inputRef}
        type="file"
        accept=".csv,.xlsx,.xlsm,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0]
          if (f) void choose(f)
        }}
      />

      {error && (
        <Card className="mb-4 border-accent/40 bg-accent-soft text-sm">{error}</Card>
      )}

      {/* ------------------------------------------------ upload dropzone -- */}
      {!preview && (
        <div
          onDragOver={(e) => {
            e.preventDefault()
            setDragging(true)
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault()
            setDragging(false)
            const f = e.dataTransfer.files?.[0]
            if (f) void choose(f)
          }}
          className={`mb-6 rounded-xl border-2 border-dashed p-8 text-center transition-colors ${
            dragging ? 'border-accent bg-accent-soft' : 'border-hairline bg-surface'
          }`}
        >
          <p className="text-sm font-medium">
            {busy ? 'Reading your sheet…' : 'Drop an Excel or CSV price list here'}
          </p>
          <p className="mx-auto mt-1 max-w-lg text-xs text-ink-soft">
            Your own column names are fine — “MRP”, “Rate”, “Cost Price”, “Particulars”
            are all understood. Nothing is saved until you have seen what will land.
          </p>
        </div>
      )}

      {/* ---------------------------------------------------- preview step -- */}
      {preview && (
        <Card className="mb-6">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="font-medium">Check before importing</h2>
              <p className="text-sm text-ink-soft">
                <span className="text-pass">{preview.accepted} ready</span>
                {preview.rejected > 0 && (
                  <> · <span className="text-fail">{preview.rejected} refused</span></>
                )}{' '}
                out of {preview.total} rows.
              </p>
            </div>
            <div className="flex gap-2">
              <Button
                variant="ghost"
                onClick={() => {
                  setPreview(null)
                  setPendingFile(null)
                  if (inputRef.current) inputRef.current.value = ''
                }}
              >
                Cancel
              </Button>
              <Button onClick={() => void confirmImport()} disabled={busy || preview.accepted === 0}>
                {busy ? 'Importing…' : `Import ${preview.accepted}`}
              </Button>
            </div>
          </div>

          <p className="mb-3 text-xs text-ink-soft">
            Matched your columns:{' '}
            {Object.entries(preview.mapping)
              .map(([k, v]) => `${v} → ${k.replace('_paise', '')}`)
              .join(' · ')}
          </p>

          <label className="mb-3 flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={replace}
              onChange={(e) => setReplace(e.target.checked)}
            />
            Retire products that are not in this sheet
            <span className="text-xs text-ink-soft">
              (they stop being offered; nothing is deleted)
            </span>
          </label>

          <div className="overflow-x-auto rounded-lg border border-hairline">
            <table className="w-full text-sm">
              <thead className="bg-sunk text-left text-[11px] uppercase tracking-wide text-ink-soft">
                <tr>
                  <th className="px-3 py-2">Line</th>
                  <th className="px-3 py-2">Code</th>
                  <th className="px-3 py-2">Product</th>
                  <th className="px-3 py-2 text-right">Price</th>
                  <th className="px-3 py-2 text-right">Cost</th>
                  <th className="px-3 py-2 text-right">Margin</th>
                  <th className="px-3 py-2">Status</th>
                </tr>
              </thead>
              <tbody>
                {preview.rows.map((r) => (
                  <tr
                    key={r.line}
                    className={`border-t border-hairline ${r.ok ? '' : 'bg-fail-bg'}`}
                  >
                    <td className="tnum px-3 py-2 text-ink-soft">{r.line}</td>
                    <td className="px-3 py-2 font-mono text-xs">{r.sku || '—'}</td>
                    <td className="px-3 py-2">{r.name || '—'}</td>
                    <td className="px-3 py-2 text-right"><Money paise={r.price_paise} /></td>
                    <td className="px-3 py-2 text-right"><Money paise={r.cost_paise} /></td>
                    <td className="px-3 py-2 text-right">
                      {r.ok ? <MarginCell bps={r.margin_bps} /> : '—'}
                    </td>
                    <td className="px-3 py-2 text-xs">
                      {r.ok ? (
                        <span className="text-pass">ready</span>
                      ) : (
                        <span className="text-fail">{r.errors.join('; ')}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {/* --------------------------------------------------- current list -- */}
      {items?.length === 0 && !preview && (
        <EmptyState
          title="No products yet"
          body="Upload your price list to get started. The assistant can only ever quote products it finds here."
          action={<Button onClick={() => inputRef.current?.click()}>Upload a sheet</Button>}
        />
      )}

      {items && items.length > 0 && (
        <Card>
          <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
            <h2 className="font-medium">{items.length} products</h2>
            <p className="text-xs text-ink-soft">
              Cost, margin and ceilings are yours alone — never shown to a customer.
            </p>
          </div>

          {/* What the two ceiling columns are worked out from. Sitting above
              the table rather than on a settings screen because the whole
              point is watching the numbers move as you change them. */}
          <div className="mb-4 flex flex-wrap items-end gap-3 rounded-xl border border-hairline bg-sunk px-4 py-3">
            <label className="block">
              <span className="mb-1 block text-2xs font-semibold uppercase tracking-[0.1em] text-ink-soft">
                Max discount
              </span>
              <div className="flex items-baseline gap-1">
                <input
                  className="w-16 rounded-lg border border-hairline bg-card px-2 py-1 text-mini text-ink outline-none focus:border-accent"
                  inputMode="decimal"
                  value={maxDiscount}
                  onChange={(e) => setMaxDiscount(e.target.value)}
                />
                <span className="text-tiny text-ink-soft">%</span>
              </div>
            </label>
            <label className="block">
              <span className="mb-1 block text-2xs font-semibold uppercase tracking-[0.1em] text-ink-soft">
                Margin floor
              </span>
              <div className="flex items-baseline gap-1">
                <input
                  className="w-16 rounded-lg border border-hairline bg-card px-2 py-1 text-mini text-ink outline-none focus:border-accent"
                  inputMode="decimal"
                  value={marginFloor}
                  onChange={(e) => setMarginFloor(e.target.value)}
                />
                <span className="text-tiny text-ink-soft">%</span>
              </div>
            </label>
            <label className="block">
              <span className="mb-1 block text-2xs font-semibold uppercase tracking-[0.1em] text-ink-soft">
                New customers get
              </span>
              <div className="flex items-baseline gap-1">
                <input
                  className="w-16 rounded-lg border border-hairline bg-card px-2 py-1 text-mini text-ink outline-none focus:border-accent"
                  inputMode="numeric"
                  value={newBandPct}
                  onChange={(e) => setNewBandPct(e.target.value)}
                />
                <span className="text-tiny text-ink-soft">% of the ceiling</span>
              </div>
            </label>
            <p className="ml-auto max-w-sm text-tiny leading-relaxed text-ink-soft">
              These are the same numbers the campaign preview uses, so a ceiling
              here is the one that will actually be committed.
            </p>
          </div>
          <div className="overflow-x-auto rounded-lg border border-hairline">
            <table className="w-full text-sm">
              <thead className="bg-sunk text-left text-[11px] uppercase tracking-wide text-ink-soft">
                <tr>
                  <th className="px-3 py-2">Code</th>
                  <th className="px-3 py-2">Product</th>
                  <th className="px-3 py-2">Unit</th>
                  <th className="px-3 py-2 text-right">Price</th>
                  <th className="px-3 py-2 text-right">Cost 🔒</th>
                  <th className="px-3 py-2 text-right">Margin 🔒</th>
                  <th className="px-3 py-2 text-right">Regular 🔒</th>
                  <th className="px-3 py-2 text-right">New 🔒</th>
                </tr>
              </thead>
              <tbody>
                {items.map((i) => {
                  const cap = capsReady ? caps.get(i.sku) : undefined
                  return (
                    <tr
                      key={i.sku}
                      className={[
                        'border-t border-hairline',
                        // A product nothing can be taken off is worth seeing
                        // at a glance, not worth hunting for in a column.
                        cap && !cap.discountable ? 'bg-fail-bg/40' : '',
                      ].join(' ')}
                    >
                      <td className="px-3 py-2 font-mono text-xs">{i.sku}</td>
                      <td className="px-3 py-2">{i.name}</td>
                      <td className="px-3 py-2 text-ink-soft">{i.unit}</td>
                      <td className="px-3 py-2 text-right"><Money paise={i.price_paise} /></td>
                      <td className="bg-sunk/50 px-3 py-2 text-right text-ink-soft">
                        <Money paise={i.cost_paise} />
                      </td>
                      <td className="bg-sunk/50 px-3 py-2 text-right">
                        <MarginCell bps={i.margin_bps} />
                      </td>
                      <td className="bg-sunk/50 px-3 py-2 text-right">
                        {cap ? (
                          <CapCell capBps={cap.max_discount_bps} discountable={cap.discountable} />
                        ) : (
                          <span className="text-ink-faint">—</span>
                        )}
                      </td>
                      <td className="bg-sunk/50 px-3 py-2 text-right">
                        {cap && cap.discountable ? (
                          <span className="tnum font-mono text-mini text-ink-soft">
                            <Pct bps={bandCapBps(cap.max_discount_bps, bandBps)} />
                          </span>
                        ) : (
                          <span className="text-ink-faint">—</span>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </MerchantShell>
  )
}
