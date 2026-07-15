import { defineConfig } from 'vite';
import { fontsPlugin } from './scripts/fonts-plugin.js';

// Tauri serves the frontend over a fixed dev port and consumes the production
// build from `dist/`. No cross-origin isolation headers are required because
// rendering runs through the native FFmpeg sidecar rather than ffmpeg.wasm.
export default defineConfig({
  clearScreen: false,
  plugins: [fontsPlugin()],
  server: {
    port: 1420,
    strictPort: true,
  },
  build: {
    target: 'es2022',
    emptyOutDir: true,
  },
});
