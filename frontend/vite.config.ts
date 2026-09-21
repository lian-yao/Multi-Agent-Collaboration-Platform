import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        /**
         * Markdown 渲染栈单独成 chunk。
         *
         * `react-markdown` + `remark-gfm` 会连整棵 `micromark` 解析器一起拖进来，约占主包
         * 三分之一（实测主包 402 kB → 589 kB，越过 Vite 的 500 kB 警戒线）。它是**静态
         * import**，所以拆出去不会多一次串行往返（Vite 会一起 modulepreload），但能让
         * 主包回到警戒线以下，且改业务代码不会让这一段缓存失效。
         *
         * 拆分依据是「这堆包只在渲染消息正文时用到」，不是「太大就拆」——再往里塞别的
         * 依赖会把这层语义冲淡。
         */
        manualChunks: {
          markdown: ["react-markdown", "remark-gfm"],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
