/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

/**
 * Vite dev-server proxy: the browser calls same-origin "/api/..." and
 * requests are forwarded to the read-only FastAPI server (Stage 6), so
 * no CORS configuration is needed. Point TENDERSCOUT_API at a
 * different API instance when needed.
 */
const API_PROXY_TARGET = process.env.TENDERSCOUT_API ?? "http://127.0.0.1:8017";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": {
        target: API_PROXY_TARGET,
        changeOrigin: true,
        rewrite: (path: string) => path.replace(/^\/api/, ""),
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/setupTests.ts",
  },
});
