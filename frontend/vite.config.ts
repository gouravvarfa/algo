/// <reference types="vitest" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The app is served by FastAPI under /app/ (see app/main.py). In development `npm run dev` proxies the API to the running server.
export default defineConfig({
  base: '/app/',
  plugins: [react()],
  build: { outDir: 'dist', sourcemap: false, chunkSizeWarningLimit: 900 },
  server: { port: 5173, proxy: { '/api': 'http://127.0.0.1:8020', '/healthz': 'http://127.0.0.1:8020', '/metrics': 'http://127.0.0.1:8020' } },
  test: { globals: true, environment: 'jsdom', setupFiles: './src/test/setup.ts', css: false },
})
