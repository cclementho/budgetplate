import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies /api to the FastAPI backend so the frontend never needs
// to know the backend URL during local development.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
