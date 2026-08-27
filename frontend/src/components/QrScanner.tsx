import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Merchant-side scanner.
 *
 * Manual entry is a PERMANENT SIBLING, not a fallback that appears after the
 * camera fails. On a counter, under shop lighting, with a cracked screen held
 * at an angle, typing twelve characters is often simply faster — and a UI that
 * only offers the keyboard after the camera has already failed has already
 * wasted the merchant's time.
 *
 * `BarcodeDetector` rather than a QR library: it is native in Android Chrome
 * (the demo handset), costs nothing in bundle size, and the typed input covers
 * every browser that lacks it. html5-qrcode was the obvious pick and is dead —
 * last release 2023, 444 open issues, known iOS Safari breakage.
 *
 * getUserMedia is called inside the tap handler. The permission dialog does
 * not itself count as a user gesture on iOS, so starting on mount is
 * unreliable. facingMode uses `ideal`, never `exact`: `exact` throws
 * OverconstrainedError on a device with no rear camera.
 */

type Detector = {
  detect: (source: CanvasImageSource) => Promise<{ rawValue: string }[]>
}

declare global {
  interface Window {
    BarcodeDetector?: new (opts?: { formats?: string[] }) => Detector
  }
}

export default function QrScanner({
  onToken,
  busy = false,
}: {
  onToken: (raw: string) => void
  busy?: boolean
}) {
  const [camOn, setCamOn] = useState(false)
  const [camErr, setCamErr] = useState<string | null>(null)
  const [typed, setTyped] = useState('')

  const videoRef = useRef<HTMLVideoElement>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const rafRef = useRef<number | null>(null)
  const lastRef = useRef<string>('')

  const stop = useCallback(() => {
    if (rafRef.current !== null) cancelAnimationFrame(rafRef.current)
    rafRef.current = null
    streamRef.current?.getTracks().forEach((t) => t.stop())
    streamRef.current = null
    setCamOn(false)
  }, [])

  useEffect(() => stop, [stop])

  const scanLoop = useCallback(
    (detector: Detector) => {
      const tick = async () => {
        const video = videoRef.current
        if (video && video.readyState === video.HAVE_ENOUGH_DATA) {
          try {
            const hits = await detector.detect(video)
            const raw = hits[0]?.rawValue
            // De-dupe: the detector fires many times per second on a held code.
            if (raw && raw !== lastRef.current) {
              lastRef.current = raw
              onToken(raw)
            }
          } catch {
            // A single bad frame is not a failure worth surfacing.
          }
        }
        rafRef.current = requestAnimationFrame(() => void tick())
      }
      rafRef.current = requestAnimationFrame(() => void tick())
    },
    [onToken],
  )

  async function start() {
    setCamErr(null)
    if (!window.BarcodeDetector) {
      setCamErr(
        'This browser cannot decode QR codes. Type the code instead — it works everywhere.',
      )
      return
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: 'environment' } },
      })
      streamRef.current = stream
      if (videoRef.current) {
        videoRef.current.srcObject = stream
        await videoRef.current.play()
      }
      setCamOn(true)
      scanLoop(new window.BarcodeDetector({ formats: ['qr_code'] }))
    } catch (e) {
      const err = e as DOMException
      setCamErr(
        err.name === 'NotAllowedError'
          ? 'Camera permission was refused. Allow it in the address bar, or type the code below.'
          : err.name === 'NotFoundError'
            ? 'No camera on this device. Type the code below.'
            : `Camera failed (${err.name}). Type the code below.`,
      )
    }
  }

  return (
    <div className="space-y-3">
      <div className="relative overflow-hidden rounded-2xl border border-hairline bg-black">
        <video
          ref={videoRef}
          playsInline
          muted
          className={`aspect-[4/3] w-full object-cover ${camOn ? '' : 'opacity-0'}`}
        />
        {!camOn && (
          <div className="absolute inset-0 flex items-center justify-center">
            <button
              type="button"
              onClick={() => void start()}
              className="rounded-xl bg-white px-4 py-2.5 text-sm font-semibold text-ink"
            >
              Start camera
            </button>
          </div>
        )}
        {camOn && (
          <button
            type="button"
            onClick={stop}
            className="absolute right-2 top-2 rounded-lg bg-black/60 px-2.5 py-1 text-xs text-white"
          >
            Stop
          </button>
        )}
      </div>

      {camErr && <p className="text-sm leading-relaxed text-fail">{camErr}</p>}

      <form
        onSubmit={(e) => {
          e.preventDefault()
          const v = typed.trim()
          if (v) {
            lastRef.current = ''
            onToken(v)
          }
        }}
        className="flex gap-2"
      >
        <input
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          placeholder="or type the code"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          className="min-w-0 flex-1 rounded-xl border border-hairline px-3 py-2.5
                     font-mono text-base tracking-wide"
        />
        <button
          type="submit"
          disabled={busy || !typed.trim()}
          className="rounded-xl bg-accent px-4 py-2.5 text-sm font-semibold text-white
                     disabled:opacity-40"
        >
          {busy ? '…' : 'Check'}
        </button>
      </form>
    </div>
  )
}
