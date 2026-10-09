import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

// In dev (`npm run dev` on the Mac), proxy API and WebSocket calls to the Pi.
const core = process.env.TACO_CORE || 'http://192.168.4.239:8700';

export default defineConfig({
  plugins: [svelte()],
  server: {
    host: true,
    proxy: {
      '/api': core,
      '/ws': { target: core.replace(/^http/, 'ws'), ws: true },
    },
  },
});
