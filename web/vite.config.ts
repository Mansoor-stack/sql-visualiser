import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The production build is bundled inside the Python package (sqlviz/static).
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../backend/sqlviz/static", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8080" } },
});
