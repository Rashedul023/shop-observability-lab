import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development (npm run dev) API calls are proxied to the order-service.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": "http://localhost:8000" },
  },
});
