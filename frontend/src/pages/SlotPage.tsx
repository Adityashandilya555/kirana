import { useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { API_BASE, apiGet } from '../lib/api'

/**
 * PHASE 0 STUB. This exists to prove the rails, not to be the chat UI.
 * It answers three questions at once, from the phone:
 *   1. does the Vercel SPA rewrite serve a DEEP LINK (/s/<token>)?
 *   2. does a cross-origin call to Railway work (CORS + $PORT + Supabase)?
 *   3. does getUserMedia open the rear camera on this handset?
 */
export default function SlotPage() {
  const { token } = useParams<{ token: string }>()
  const [health, setHealth] = useState<unknown>(null)
  const [healthErr, setHealthErr] = useState<string | null>(null)
  const [camErr, setCamErr] = useState<string | null>(null)
  const [camOn, setCamOn] = useState(false)
  const videoRef = useRef<HTMLVideoElement>(null)
  const streamRef = useRef<MediaStream | null>(null)

  useEffect(() => {
    apiGet('/health/deep').then(setHealth).catch((e) => setHealthErr(String(e)))
    return () => streamRef.current?.getTracks().forEach((t) => t.stop())
  }, [])

  // getUserMedia must be called inside the tap handler. On iOS the camera
  // permission dialog does NOT itself count as a user gesture, so calling
  // this on mount is unreliable. facingMode uses `ideal`, never `exact` --
  // `exact` throws OverconstrainedError on devices with no rear camera.
  async function startCamera() {
    setCamErr(null)
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: 'environment' } },
      })
      streamRef.current = stream
      if (videoRef.current) videoRef.current.srcObject = stream
      setCamOn(true)
    } catch (e) {
      const err = e as DOMException
      setCamErr(
        err.name === 'NotAllowedError'
          ? 'Camera denied. On Android: site settings -> Permissions -> Camera.'
          : `${err.name}: ${err.message}`,
      )
    }
  }

  const Row = ({ label, ok, children }: { label: string; ok: boolean | null; children: React.ReactNode }) => (
    <div className="border border-hairline rounded p-3">
      <div className="flex items-center gap-2 text-sm font-medium">
        <span>{ok === null ? '…' : ok ? '✅' : '❌'}</span>
        <span>{label}</span>
      </div>
      <div className="mt-1 text-xs text-ink-soft break-all font-mono">{children}</div>
    </div>
  )

  return (
    <main className="mx-auto max-w-md p-4 flex flex-col gap-3">
      <h1 className="text-xl font-bold">Kirana Agent — Phase 0</h1>

      <Row label="Deep link resolved (SPA rewrite)" ok={!!token}>
        slot token: <b>{token ?? '(none)'}</b>
      </Row>

      <Row label="Backend reachable (CORS + DB)" ok={healthErr ? false : health ? true : null}>
        {API_BASE}/health/deep
        <br />
        {healthErr ?? (health ? JSON.stringify(health) : 'checking…')}
      </Row>

      <Row label="Rear camera" ok={camErr ? false : camOn ? true : null}>
        {camErr ?? (camOn ? 'stream live' : 'not started')}
      </Row>

      {!camOn && (
        <button
          onClick={startCamera}
          className="rounded bg-accent px-4 py-3 text-white font-medium"
        >
          Start camera
        </button>
      )}

      {/* playsinline + muted are required or iOS hijacks this into the
          fullscreen native player and the overlay breaks. */}
      <video
        ref={videoRef}
        playsInline
        muted
        autoPlay
        className={`w-full rounded border border-hairline ${camOn ? '' : 'hidden'}`}
      />
    </main>
  )
}
