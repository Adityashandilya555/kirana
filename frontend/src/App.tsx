import { Navigate, Route, Routes } from 'react-router-dom'
import SlotPage from './pages/SlotPage'

export default function App() {
  return (
    <Routes>
      {/* The printed QR deep-links here. Without the vercel.json rewrite
          this path 404s on a cold load, which is the single most likely
          way this demo fails in public. */}
      <Route path="/s/:token" element={<SlotPage />} />
      <Route path="*" element={<Navigate to="/s/PHASE0TEST" replace />} />
    </Routes>
  )
}
