import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The SPA always calls `/api/*`; dev and the nginx image both strip the prefix and
// forward to the platform API, so the browser never needs CORS. `/tiles/*` and `/scenes/*` go to
// the tiler as they are.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.INGEST_API_URL ?? 'http://localhost:8000',
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
      // Preview tiles, as nginx routes them: the tiler serves `/tiles/...` itself.
      '/tiles': process.env.INGEST_TILER_URL ?? 'http://localhost:8001',
      // A mesh's 3D Tiles, likewise.
      '/scenes': process.env.INGEST_TILER_URL ?? 'http://localhost:8001',
    },
  },
})
