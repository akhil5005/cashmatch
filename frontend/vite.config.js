import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    // The API runs on another port in development; proxying keeps the
    // frontend talking to a same-origin /api in every environment.
    proxy: {
      "/api": { target: process.env.VITE_API_URL || "http://localhost:8000", changeOrigin: true },
    },
  },
});
