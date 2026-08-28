import { useCallback, useEffect, useRef, useState } from 'react'
import MerchantShell from '../../components/MerchantShell'
import { Button, Card, EmptyState, Money, Pct } from '../../components/ui'
import { API_BASE, MERCHANT_KEY } from '../../lib/api'
import { getCatalog, importSheet, previewSheet } from '../../lib/merchant'
import type { CatalogItem, ParsedSheet } from '../../lib/merchant'

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

export default function CatalogPage() {
  const [items, setItems] = useState<CatalogItem[] | null>(null)
  const [preview, setPreview] = useState<ParsedSheet | null>(null)
  const [pendingFile, setPendingFile] = useState<File | null>(null)
  const [replace, setReplace] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const load = useCallback(() => {
    getCatalog()
      .then((r) => setItems(r.items))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [])

  useEffect(load, [load])

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
          <div className="mb-3 flex items-baseline justify-between">
            <h2 className="font-medium">{items.length} products</h2>
            <p className="text-xs text-ink-soft">
              Cost and margin are yours alone — never shown to a customer.
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
                </tr>
              </thead>
              <tbody>
                {items.map((i) => (
                  <tr key={i.sku} className="border-t border-hairline">
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
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </MerchantShell>
  )
}
