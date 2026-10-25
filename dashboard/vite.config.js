// Vite dev server for the dashboard. Requests to /api go to the FastAPI backend (uvicorn api.main:app on :8000).
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": "http://localhost:8000" },
  },
});
