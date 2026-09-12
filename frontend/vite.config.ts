import { defineConfig } from "vite"
import vue from "@vitejs/plugin-vue"

export default defineConfig({
  plugins: [vue()],
  base: "./", // file:// loading inside Qt WebEngine requires relative asset paths
  build: { outDir: "dist", assetsInlineLimit: 0 },
  test: { environment: "jsdom" },
})
