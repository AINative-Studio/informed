import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react()],
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
