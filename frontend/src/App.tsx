import { Navigate, Route, Routes } from 'react-router-dom'
import LandingPage from './pages/LandingPage'
import MerchantConsole from './pages/MerchantConsole'
import RedemptionPage from './pages/RedemptionPage'
import SlotPage from './pages/SlotPage'
import VerifyPage from './pages/VerifyPage'

export default function App() {
  return (
    <Routes>
      {/* The front door. Previously this redirected to /s/PHASE0TEST, a Phase 0
          placeholder that is not a real slot -- so the bare domain rendered an
          error screen. */}
      <Route path="/" element={<LandingPage />} />
      {/* The printed QR deep-links here. Without the vercel.json rewrite
          this path 404s on a cold load, which is the single most likely
          way this demo fails in public. */}
      <Route path="/s/:token" element={<SlotPage />} />
      {/* The customer's screen after paying: the QR the merchant scans, plus
          an independent browser-side proof check. Reads, never burns. */}
      <Route path="/r/:token" element={<RedemptionPage />} />
      {/* The counter. First scan green, every later scan red. */}
      <Route path="/verify" element={<VerifyPage />} />
      {/* Projected, never mirrored from the phone: Android payment apps set
          FLAG_SECURE and screen-share as a black rectangle. */}
      <Route path="/merchant/:campaignId" element={<MerchantConsole />} />
      <Route path="/merchant" element={<MerchantConsole />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
