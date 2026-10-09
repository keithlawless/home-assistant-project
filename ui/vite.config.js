import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

// In dev (`npm run dev` on the Mac), proxy API and WebSocket calls to the Pi.
// taco-core only listens on the Pi's localhost, so tunnel it first:
//   ssh -N -L 8700:127.0.0.1:8700 taco@192.168.4.239
const core = process.env.TACO_CORE || 'http://127.0.0.1:8700';

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
