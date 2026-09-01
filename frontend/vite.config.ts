import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// 构建产物直接落到 askdb/web —— FastAPI 从那里托管，Dockerfile 只 COPY askdb/，
// 镜像里因此不需要 node。产物入 git，部署链路与换壳前保持一致。
export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    outDir: '../askdb/web',
    emptyOutDir: true,
  },
  server: {
    // 开发时前端跑在 5173，接口打到本地 askdb 服务
    proxy: { '/api': 'http://127.0.0.1:8011' },
  },
})
