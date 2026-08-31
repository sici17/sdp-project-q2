import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    /*
      Never inline a font as a `data:` URI.

      The app is served under `default-src 'self'` with no `font-src`, so a
      data: font is blocked outright and the type silently falls back - which
      only shows up behind nginx, never on the dev server. Vite inlines any
      asset under 4 KiB by default, which caught the small Latin subsets of all
      three families. Returning false forces those to stay real files, so they
      load as 'self' and the CSP needs no new source.
    */
    assetsInlineLimit: (filePath: string) =>
      /\.(woff2?|ttf|otf|eot)$/i.test(filePath) ? false : undefined,
  },
  server: {
    proxy: {
      '/api': {
        target: process.env.GATEWAY_PROXY_TARGET ?? 'http://127.0.0.1:8080',
        changeOrigin: true,
      },
      '/manuals': {
        target: process.env.GATEWAY_PROXY_TARGET ?? 'http://127.0.0.1:8080',
        changeOrigin: true,
      },
      '/metrics': {
        target: process.env.GATEWAY_PROXY_TARGET ?? 'http://127.0.0.1:8080',
        changeOrigin: true,
      },
    },
  },
})
