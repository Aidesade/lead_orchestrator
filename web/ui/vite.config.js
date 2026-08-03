import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// В dev фронт живёт на 5173 и ходит в FastAPI через прокси — так тот же самый /api/*
// работает и в проде, где FastAPI сам раздаёт собранный dist (никаких base-url в коде).
const API = process.env.VITE_API || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: API,
        changeOrigin: true,
        // SSE не должен буферизоваться прокси, иначе прогресс приезжает пачкой в конце.
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            if (String(proxyRes.headers['content-type']).includes('event-stream')) {
              proxyRes.headers['cache-control'] = 'no-cache'
            }
          })
        },
      },
    },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
