import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: 'localhost',
    port: 5173,
    strictPort: true,
    // proxy 是“代理”：开发时由 Vite 帮浏览器把指定请求转发给后端，
    // 再把后端的响应传回浏览器。这里的后端运行在本机 8000 端口。
    // 例如：浏览器请求 http://localhost:5173/health，
    // Vite 会转发到 http://127.0.0.1:8000/health。
    // 没有配置路径改写，所以转发时保留原来的 /health 或 /api/v1/... 路径。
    // 这只是本地开发服务的代理，不会随打包文件自动部署到线上。
    proxy: {
      // 健康检查请求：确认后端 HTTP 服务能否正常回答。
      '/health': 'http://127.0.0.1:8000',
      // 以 /api/v1 开头的业务请求也转发给后端，例如 /api/v1/users/me。
      // 这里只负责转发，不会自动创建这些接口；目前业务接口尚未实现。
      '/api/v1': 'http://127.0.0.1:8000',
    },
  },
})
