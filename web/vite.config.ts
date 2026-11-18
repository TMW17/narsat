import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// In development the browser talks to Vite only; Vite forwards API and raster-tile
// requests to the services started by `docker compose` (or run by hand), so the
// application needs no CORS configuration and uses the same relative URLs as in
// production behind nginx.
const API_TARGET = process.env.NARSAT_DEV_API ?? "http://localhost:8000";
const TITILER_TARGET = process.env.NARSAT_DEV_TITILER ?? "http://localhost:8083";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: API_TARGET, changeOrigin: true },
      "/titiler": {
        target: TITILER_TARGET,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/titiler/, ""),
      },
    },
  },
  build: {
    sourcemap: true,
    chunkSizeWarningLimit: 1500,
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
