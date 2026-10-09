import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const apiTarget = process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000'
const syncTarget = process.env.VITE_SYNC_PROXY_TARGET || 'http://127.0.0.1:8787'

export default defineConfig({
  plugins: [vue()],
  resolve: { alias: { '@': '/src' } },
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: { '/api': apiTarget, '/health': apiTarget, '/rooms': { target: syncTarget, ws: true } },
  },
})
