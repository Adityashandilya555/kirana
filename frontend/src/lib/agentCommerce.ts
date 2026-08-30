/**
 * The shop's machine-readable front door.
 *
 * These endpoints are deliberately unauthenticated and deliberately not under
 * /api/v1/campaigns: they exist so that somebody else's AI agent -- one this
 * shop has never heard of -- can discover the shop, ask for a price, and check
 * the answer without trusting the connection it arrived over.
 *
 * They have been live since Phase D with nothing in the console pointing at
 * them, which meant the feature could not be shown to anyone. This module is
 * the read side of that.
 */

import { API_BASE } from './api'

export interface AgentManifest {
  protocol: string
  merchant: { name: string; description?: string }
  currency: string
  endpoints: { catalog: string; quote: string; verify: string }
  capabilities: {
    bounded_discount?: {
      description: string
      /** The Merkle root every live campaign's ceilings are committed under.
       *  This is the number that makes the discount claim checkable. */
      commitment_root: string
      proof: string
    }
  }
  signing: {
    alg: string
    /** Base64 Ed25519. Empty when AGENT_SIGNING_SECRET_KEY is unset, in which
     *  case quotes are served but marked signed:false. */
    public_key: string
    canonicalisation: string
  }
  quote_policy: {
    ttl_seconds: number
    reserves_budget: boolean
    revalidated_on_accept: boolean
  }
}

/** Note the field name. The agent catalog publishes `list_price_paise`, not
 *  the `price_paise` the merchant-side catalog uses -- what an outside agent
 *  is quoted is the list price, before any campaign discount is negotiated.
 *  Getting this wrong renders an empty column rather than an error, so it was
 *  checked against the live endpoint rather than assumed. */
export interface AgentCatalogItem {
  sku: string
  name: string
  unit?: string
  available: boolean
  list_price_paise: number
}

export interface AgentCatalog {
  merchant?: { name: string }
  currency?: string
  items: AgentCatalogItem[]
}

/** The discovery document, at the well-known path an agent would probe. */
export const getManifest = async (): Promise<AgentManifest> => {
  const res = await fetch(`${API_BASE}/.well-known/agent-commerce.json`)
  if (!res.ok) throw new Error(`Discovery document returned ${res.status}`)
  return res.json() as Promise<AgentManifest>
}

export const getAgentCatalog = async (): Promise<AgentCatalog> => {
  const res = await fetch(`${API_BASE}/api/v1/agent/catalog`)
  if (!res.ok) throw new Error(`Agent catalog returned ${res.status}`)
  return res.json() as Promise<AgentCatalog>
}

/** The public URL of the discovery document, for showing and for copying. */
export const manifestUrl = () => `${API_BASE}/.well-known/agent-commerce.json`
