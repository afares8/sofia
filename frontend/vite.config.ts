import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    // Emit .map files without a sourceMappingURL comment so Sofia can decode
    // production stacks while maps stay out of the browser's fetch path.
    sourcemap: 'hidden',
  },
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
