import { defineConfig } from 'vite';

export default defineConfig({
  base: '/chat/preview/',
  build: {
    outDir: '../src/ai_team/web/modern',
    emptyOutDir: true,
    modulePreload: { polyfill: false },
  },
});
