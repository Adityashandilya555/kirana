import { useCallback, useEffect, useState } from 'react'
import MerchantShell, {
  Button,
  Card,
  EmptyState,
  Field,
  inputClass,
} from '../../components/MerchantShell'
import { deleteShelf, getCatalog, getShelves, saveShelf } from '../../lib/merchant'
import type { CatalogItem, Shelf } from '../../lib/merchant'

/**
 * Shelves: a named group of products a sticker can be scoped to.
 *
 * The point is not organisation for its own sake. A sticker bound to a shelf
 * makes the assistant's world exactly that shelf — it is not told to avoid
 * other products, it simply never learns they exist. So a shelf is a
 * capability boundary, and the editor should feel closer to choosing
 * permissions than to tagging.
 */

export default function ShelvesPage() {
  const [shelves, setShelves] = useState<Shelf[] | null>(null)
  const [catalog, setCatalog] = useState<CatalogItem[]>([])
  const [editing, setEditing] = useState<Shelf | null>(null)
  const [name, setName] = useState('')
  const [note, setNote] = useState('')
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    Promise.all([getShelves(), getCatalog()])
      .then(([s, c]) => {
        setShelves(s.shelves)
        setCatalog(c.items)
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [])

  useEffect(load, [load])

  function startNew() {
    setEditing({ id: '', name: '', note: '', skus: [], item_count: 0 })
    setName('')
    setNote('')
    setPicked(new Set())
  }

  function startEdit(s: Shelf) {
    setEditing(s)
    setName(s.name)
    setNote(s.note)
    setPicked(new Set(s.skus))
  }

  async function save() {
    if (!name.trim() || picked.size === 0) return
    setBusy(true)
    setError(null)
    try {
      await saveShelf({
        name: name.trim(),
        note: note.trim(),
        skus: [...picked],
        shelf_id: editing?.id || null,
      })
      setEditing(null)
      load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function remove(s: Shelf) {
    if (!confirm(`Remove the “${s.name}” shelf? Stickers already printed for it keep working.`))
      return
    try {
      await deleteShelf(s.id)
      load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <MerchantShell
      title="Shelves"
      subtitle="Group products so a sticker can cover a whole shelf instead of one item."
      actions={
        <Button onClick={startNew} disabled={catalog.length === 0}>
          New shelf
        </Button>
      }
    >
      {error && (
        <Card className="mb-4 border-fail/40 bg-fail-soft text-sm text-fail">{error}</Card>
      )}

      {catalog.length === 0 && (
        <EmptyState
          title="Add products first"
          body="A shelf is a selection of your products, so there is nothing to choose from yet."
        />
      )}

      {/* ------------------------------------------------------- editor -- */}
      {editing && (
        <Card className="mb-6">
          <h2 className="mb-4 font-medium">
            {editing.id ? `Edit “${editing.name}”` : 'New shelf'}
          </h2>
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="Shelf name" hint="What you would call it out loud — “Tea & Beverages”.">
              <input
                className={inputClass}
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Tea & Beverages"
              />
            </Field>
            <Field label="Note" hint="Optional. Where it is, for your own reference.">
              <input
                className={inputClass}
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="End cap near the till"
              />
            </Field>
          </div>

          <p className="mb-2 mt-5 text-sm font-medium">
            Products on this shelf{' '}
            <span className="font-normal text-ink-soft">({picked.size} selected)</span>
          </p>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {catalog.map((c) => {
              const on = picked.has(c.sku)
              return (
                <button
                  key={c.sku}
                  type="button"
                  onClick={() =>
                    setPicked((p) => {
                      const next = new Set(p)
                      if (next.has(c.sku)) next.delete(c.sku)
                      else next.add(c.sku)
                      return next
                    })
                  }
                  className={`rounded-lg border px-3 py-2 text-left text-sm transition-colors ${
                    on
                      ? 'border-accent bg-accent-soft'
                      : 'border-hairline bg-surface hover:bg-sunk'
                  }`}
                >
                  <span className="block truncate font-medium">{c.name}</span>
                  <span className="font-mono text-[11px] text-ink-soft">{c.sku}</span>
                </button>
              )
            })}
          </div>

          <div className="mt-5 flex gap-2">
            <Button onClick={() => void save()} disabled={busy || !name.trim() || picked.size === 0}>
              {busy ? 'Saving…' : 'Save shelf'}
            </Button>
            <Button variant="ghost" onClick={() => setEditing(null)}>
              Cancel
            </Button>
          </div>
        </Card>
      )}

      {/* -------------------------------------------------------- list -- */}
      {shelves?.length === 0 && !editing && catalog.length > 0 && (
        <EmptyState
          title="No shelves yet"
          body="Without shelves, every sticker covers your whole catalog. Create one to scope stickers to a section of the shop."
          action={<Button onClick={startNew}>New shelf</Button>}
        />
      )}

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {shelves?.map((s) => (
          <Card key={s.id} className="flex flex-col gap-3">
            <div>
              <p className="font-medium">{s.name}</p>
              {s.note && <p className="text-xs text-ink-soft">{s.note}</p>}
            </div>
            <div className="flex flex-wrap gap-1.5">
              {s.skus.map((sku) => (
                <span
                  key={sku}
                  className="rounded-md bg-sunk px-2 py-0.5 font-mono text-[11px]"
                >
                  {sku}
                </span>
              ))}
            </div>
            <div className="mt-auto flex gap-2 pt-1">
              <Button variant="ghost" onClick={() => startEdit(s)}>
                Edit
              </Button>
              <Button variant="danger" onClick={() => void remove(s)}>
                Remove
              </Button>
            </div>
          </Card>
        ))}
      </div>
    </MerchantShell>
  )
}
