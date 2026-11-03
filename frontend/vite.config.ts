/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { SECURITY_HEADERS, contentSecurityPolicy } from "./csp";

// `vite preview` serves the production build with the same headers as nginx; a cross-origin API base URL (as in the
// E2E setup) is added to connect-src.
const apiBase = process.env.VITE_API_BASE_URL ?? "";
const apiOrigin = /^https?:\/\//.test(apiBase) ? [new URL(apiBase).origin] : [];

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173, strictPort: true },
  preview: { headers: { ...SECURITY_HEADERS, "Content-Security-Policy": contentSecurityPolicy(apiOrigin) } },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    testTimeout: 15000,
    exclude: ["node_modules", "dist", "e2e"],
  },
});
