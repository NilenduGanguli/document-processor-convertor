import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// Everything ships in frontend/dist; nothing is fetched from a CDN, a font host or an
// analytics endpoint at runtime. The only network the console touches is its own origin's
// /api — a converter that keeps documents in-network must not ship a console that phones out.
export default defineConfig({
  plugins: [react()],
  build: {
    // dist is committed: no sourcemaps, keep the diff sane.
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8300',
      '/health': 'http://localhost:8300',
      '/readyz': 'http://localhost:8300',
    },
  },
});
