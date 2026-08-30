import { useEffect, useState } from 'react'
import MerchantShell from '../../components/MerchantShell'
import { Button, Callout, Card, Eyebrow, Money, Pill } from '../../components/ui'
import { getAgentCatalog, getManifest, manifestUrl } from '../../lib/agentCommerce'
import type { AgentCatalog, AgentManifest } from '../../lib/agentCommerce'

/**
 * "Other people's AI can shop here", made visible.
 *
 * The agent-commerce endpoints have been serving since Phase D and nothing in
 * the console mentioned them, so the capability existed and could not be
 * demonstrated. This page is the demonstration: it reads the shop's own live
 * discovery document over the public path -- no merchant key, exactly the way
 * a stranger's agent would -- and shows what comes back.
 *
 * Fetching it rather than describing it is the point. A screenshot of a JSON
 * blob proves nothing; a page that goes and gets it proves the door is open
 * right now, and turns red the moment it is not.
 */

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1 border-t border-hairline py-3 first:border-t-0 sm:flex-row sm:gap-4">
      <span className="w-full shrink-0 text-tiny font-semibold text-ink sm:w-48">
        {label}
      </span>
      <div className="min-w-0 flex-1 text-mini text-ink-soft">{children}</div>
    </div>
  )
}

function Copyable({ value }: { value: string }) {
  const [done, setDone] = useState(false)
  return (
    <div className="flex items-start gap-2">
      <code className="min-w-0 flex-1 break-all rounded-lg bg-sunk px-2.5 py-1.5 font-mono text-2xs text-ink">
        {value}
      </code>
      <button
        type="button"
        className="shrink-0 rounded-lg border border-hairline bg-card px-2.5 py-1.5 text-2xs font-semibold text-ink transition-colors hover:bg-sunk"
        onClick={() => {
          // clipboard is unavailable over plain http on a LAN address, which
          // is exactly where this gets demoed. Say so instead of silently
          // doing nothing.
          navigator.clipboard
            ?.writeText(value)
            .then(() => setDone(true))
            .catch(() => setDone(false))
        }}
      >
        {done ? 'copied' : 'copy'}
      </button>
    </div>
  )
}

export default function AgentPage() {
  const [manifest, setManifest] = useState<AgentManifest | null>(null)
  const [catalog, setCatalog] = useState<AgentCatalog | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    // No setLoading(true) here: the state already initialises to true and this
    // effect runs once on mount, so setting it synchronously would only cost a
    // second render before the fetch has even started.
    let alive = true
    Promise.all([getManifest(), getAgentCatalog()])
      .then(([m, c]) => {
        if (!alive) return
        setManifest(m)
        setCatalog(c)
        setError(null)
      })
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [])

  const signed = Boolean(manifest?.signing?.public_key)
  const root = manifest?.capabilities?.bounded_discount?.commitment_root

  return (
    <MerchantShell
      eyebrow="Reach"
      title="Sell to other people's AI"
      subtitle="Your shop publishes a page that shopping assistants can read on their own — prices, and a promise about discounts they can check without taking your word for it."
    >
      {loading && (
        <Card className="text-center text-mini text-ink-soft">
          Checking your shop's front door…
        </Card>
      )}

      {error && (
        <Callout tone="fail">
          <p className="font-semibold text-fail">Your shop is not answering.</p>
          <p className="mt-1">
            An assistant trying to find you right now would get nothing. ({error})
          </p>
        </Callout>
      )}

      {manifest && (
        <div className="space-y-5">
          <Callout tone="pass">
            <p className="font-semibold">
              Live. Any assistant can find {manifest.merchant.name} and ask for a
              price.
            </p>
            <p className="mt-1">
              This page was just read from your shop over the public internet, with
              no key and no login — the same way a stranger's assistant would.
            </p>
          </Callout>

          {/* ------------------------------------------------ the address -- */}
          <Card>
            <Eyebrow>Your shop's address for machines</Eyebrow>
            <p className="mt-2 text-mini leading-relaxed text-ink-soft">
              Give this to anyone building a shopping assistant. Everything else
              is discovered from it.
            </p>
            <div className="mt-3">
              <Copyable value={manifestUrl()} />
            </div>
          </Card>

          {/* ------------------------------------------------- the promise -- */}
          <Card>
            <Eyebrow>What you are promising them</Eyebrow>
            <div className="mt-2">
              <Row label="Shop">
                {manifest.merchant.name}
                {manifest.merchant.description && (
                  <span className="text-ink-faint"> · {manifest.merchant.description}</span>
                )}
              </Row>
              <Row label="Prices quoted in">{manifest.currency}</Row>
              <Row label="A quote is good for">
                {manifest.quote_policy.ttl_seconds} seconds
                <span className="text-ink-faint">
                  {manifest.quote_policy.revalidated_on_accept
                    ? ' · re-checked again when they accept'
                    : ''}
                </span>
              </Row>
              <Row label="Discount ceiling">
                {root ? (
                  <>
                    Every discount is capped by a limit you committed to before any
                    sticker was printed, and the assistant can prove that for
                    itself.
                    <details className="mt-1.5">
                      <summary className="cursor-pointer text-2xs text-ink-soft">
                        Proof reference
                      </summary>
                      <p className="mt-1 break-all font-mono text-2xs text-ink-soft">
                        {root}
                      </p>
                    </details>
                  </>
                ) : (
                  'No live campaign, so there is no ceiling to prove yet.'
                )}
              </Row>
              <Row label="Signed quotes">
                {signed ? (
                  <Pill tone="pass" dot>
                    on
                  </Pill>
                ) : (
                  <>
                    <Pill tone="warn" dot>
                      off
                    </Pill>
                    <p className="mt-1.5">
                      Quotes still work, but they go out marked unsigned, so an
                      assistant cannot prove a price really came from you. Set
                      AGENT_SIGNING_SECRET_KEY to turn this on.
                    </p>
                  </>
                )}
              </Row>
            </div>
          </Card>

          {/* ------------------------------------------- what they can see -- */}
          <Card>
            <Eyebrow>What an assistant sees right now</Eyebrow>
            {catalog && catalog.items.length > 0 ? (
              <ul className="mt-3 divide-y divide-hairline">
                {catalog.items.slice(0, 8).map((i) => (
                  <li key={i.sku} className="flex items-baseline justify-between gap-3 py-2">
                    <span className="min-w-0 text-mini text-ink">
                      {i.name}
                      <span className="ml-1.5 font-mono text-2xs text-ink-faint">
                        {i.sku}
                      </span>
                      {!i.available && (
                        <span className="ml-1.5 text-2xs text-ink-faint">
                          (out of stock)
                        </span>
                      )}
                    </span>
                    <Money paise={i.list_price_paise} className="text-mini text-ink" />
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-2 text-mini text-ink-soft">
                Nothing yet — add products and they appear here automatically.
              </p>
            )}
            {catalog && catalog.items.length > 8 && (
              <p className="mt-2 text-tiny text-ink-soft">
                …and {catalog.items.length - 8} more.
              </p>
            )}
          </Card>

          {/* ------------------------------------------------- show anyone -- */}
          <Card>
            <Eyebrow>Showing it to someone</Eyebrow>
            <p className="mt-2 text-mini leading-relaxed text-ink-soft">
              Paste this into any terminal. It is the first thing an assistant
              does when it meets your shop.
            </p>
            <div className="mt-3">
              <Copyable value={`curl -s ${manifestUrl()}`} />
            </div>
            <div className="mt-4 flex flex-wrap gap-2">
              <a href={manifestUrl()} target="_blank" rel="noreferrer">
                <Button variant="ghost">Open the live page</Button>
              </a>
              <a href={manifest.endpoints.catalog} target="_blank" rel="noreferrer">
                <Button variant="ghost">Open the price list</Button>
              </a>
            </div>
          </Card>
        </div>
      )}
    </MerchantShell>
  )
}
