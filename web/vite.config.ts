import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The SPA always calls `/api/*`; dev and the nginx image both strip the prefix and
// forward to the platform API, so the browser never needs CORS.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.INGEST_API_URL ?? 'http://localhost:8000',
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
