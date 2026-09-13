import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Separate from vite.config.ts (which wires in `@tailwindcss/vite`, a plugin vitest's
// jsdom environment doesn't need and that only slows test startup down) -- E2.11 is this
// repo's first frontend test suite, so this file also documents the minimum setup a
// later feature's tests can build on.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
