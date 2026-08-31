import { Navigate, Route, Routes } from 'react-router-dom'
import LandingPage from './pages/LandingPage'
import RedemptionPage from './pages/RedemptionPage'
import SlotPage from './pages/SlotPage'
import VerifyPage from './pages/VerifyPage'
import AgentPage from './pages/merchant/AgentPage'
import BuilderPage from './pages/merchant/BuilderPage'
import CampaignDetailPage from './pages/merchant/CampaignDetailPage'
import CampaignsPage from './pages/merchant/CampaignsPage'
import CatalogPage from './pages/merchant/CatalogPage'
import NewCampaignPage from './pages/merchant/NewCampaignPage'
import ShelvesPage from './pages/merchant/ShelvesPage'

export default function App() {
  return (
    <Routes>
      {/* The front door. */}
      <Route path="/" element={<LandingPage />} />

      {/* Customer, mobile. The printed QR deep-links to /s/<token>; without the
          vercel.json rewrite this 404s on a cold load, which is the single most
          likely way this fails in public. */}
      <Route path="/s/:token" element={<SlotPage />} />
      <Route path="/r/:token" element={<RedemptionPage />} />

      {/* Counter. First scan green, every later scan red. */}
      <Route path="/verify" element={<VerifyPage />} />

      {/* Shopkeeper console, desktop. Static segments are listed before the
          dynamic one; React Router ranks by specificity, so /merchant/catalog
          cannot be swallowed by /merchant/:campaignId. */}
      <Route path="/merchant" element={<CampaignsPage />} />
      <Route path="/merchant/catalog" element={<CatalogPage />} />
      <Route path="/merchant/shelves" element={<ShelvesPage />} />
      <Route path="/merchant/builder" element={<BuilderPage />} />
      <Route path="/merchant/agent" element={<AgentPage />} />
      <Route path="/merchant/new" element={<NewCampaignPage />} />
      <Route path="/merchant/:campaignId" element={<CampaignDetailPage />} />

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
