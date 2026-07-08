import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 18761,
    proxy: {
      '/api': {
        target: 'http://localhost:18760',
        changeOrigin: true,
      },
      '/auth': {
        target: 'http://localhost:18760',
        changeOrigin: true,
      },
      '/mcp': {
        target: 'http://localhost:18760',
        changeOrigin: true,
      },
    },
  },
})
