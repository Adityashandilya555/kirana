import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Tailwind v4 is a Vite plugin -- no postcss.config, no tailwind.config.
export default defineConfig({
  plugins: [react(), tailwindcss()],
})
