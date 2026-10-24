import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // 打包出的资源用相对路径，同一份 dist 挂在 / 或 /ai/<id>/ 下都能用
  base: "./",
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8710",
        changeOrigin: true,
      },
    },
  },
});
