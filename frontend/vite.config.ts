import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';
import { VitePWA } from 'vite-plugin-pwa';

export default defineConfig({
  plugins: [
    react(),
    // Story 7.7: installable PWA. `vite-plugin-pwa` (wraps Workbox) is the
    // standard way to get a manifest and a generated, versioned service
    // worker out of a Vite build — not hand-rolled, so there is no custom
    // cache-invalidation logic to get wrong. `registerType: 'autoUpdate'`
    // means a new deployed version takes over on next load with no voter
    // action needed, rather than a stale cached app silently never updating.
    VitePWA({
      registerType: 'autoUpdate',
      // Precache the app shell — the HTML, JS/CSS bundles and these icons —
      // so a reload with no network (this story's "view my profile
      // offline" at the polling place) serves the shell from the service
      // worker's cache instead of failing the navigation outright. Without
      // this, `page.goto` on an offline browser fails with
      // `net::ERR_INTERNET_DISCONNECTED` before React even mounts — the
      // profile's own encrypted IndexedDB cache (`src/offline/
      // encryptedProfileCache.ts`) only ever gets a chance to answer once
      // the shell itself has loaded. Confirmed live against this app's
      // Playwright BDD suite (`tests/features/installable_pwa.feature`)
      // before this plugin was added: an offline reload failed at the
      // navigation step every time with no service worker registered.
      includeAssets: ['icon-192.png', 'icon-512.png', 'icon-512-maskable.png', 'apple-touch-icon.png'],
      manifest: {
        name: 'Informed',
        short_name: 'Informed',
        description:
          'See how closely each candidate matches what you care about, with cited evidence.',
        start_url: '/',
        scope: '/',
        display: 'standalone',
        background_color: '#ffffff',
        // Matches the icons' own civic-blue background (see
        // public/icon-*.png's generation script) so the OS splash/task
        // switcher chrome does not clash with the icon.
        theme_color: '#14416e',
        icons: [
          { src: 'icon-192.png', sizes: '192x192', type: 'image/png', purpose: 'any' },
          { src: 'icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'any' },
          { src: 'icon-512-maskable.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
        ],
      },
      workbox: {
        // This is a single-page app with no router (see `App.tsx`'s own
        // docstring) — every path should resolve to the one shell.
        navigateFallback: 'index.html',
      },
      devOptions: {
        // The service worker is a production build concern; `npm run dev`
        // (and this repo's unit/BDD suites, which run against `vite dev`'s
        // module graph or a `vite preview` of the production build) does
        // not need a dev-mode service worker, and enabling one here would
        // make the dev server behave differently from everyone's actual
        // day-to-day `npm run dev` loop for no benefit this story needs.
        enabled: false,
      },
    }),
  ],
  server: { port: 5173 },
  build: { outDir: 'dist', sourcemap: true },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./tests/setup.ts'],
    // tests/ holds suites about the test tooling itself (story 1.7's
    // pending-step idiom), which run in Node rather than jsdom.
    include: ['src/**/*.test.{ts,tsx}', 'tests/**/*.test.ts'],
    environmentMatchGlobs: [['tests/**', 'node']],
    coverage: { reporter: ['text'], include: ['src/**'] },
  },
});
