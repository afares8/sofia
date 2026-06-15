import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5179,
    strictPort: true,
    proxy: {
      // Force IPv4: on Windows "localhost" resolves to ::1 (IPv6) first, but the
      // backend binds 0.0.0.0 (IPv4 only), causing ECONNREFUSED proxy errors.
      '/api': 'http://127.0.0.1:5180',
    },
  },
})
