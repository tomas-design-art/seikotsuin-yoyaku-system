import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const apiProxyTarget = process.env.VITE_API_PROXY_TARGET || 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    allowedHosts: true,
    proxy: {
      '/api/sse': {
        target: apiProxyTarget,
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes, req, res) => {
            // プロキシでバッファリングを無効化するために、ヘッダーを上書き・追加
            proxyRes.headers['cache-control'] = 'no-cache, no-transform';
            proxyRes.headers['connection'] = 'keep-alive';
          });
        }
      },
      '/api': {
        target: apiProxyTarget,
        changeOrigin: true,
        configure: (proxy) => {
          // 本番の server.js と同じく、画面が X-Auth-Token で送るトークンを Authorization: Bearer に直す
          proxy.on('proxyReq', (proxyReq, req) => {
            const token = req.headers['x-auth-token'];
            if (typeof token === 'string' && token) {
              proxyReq.setHeader('Authorization', `Bearer ${token}`);
              proxyReq.removeHeader('X-Auth-Token');
            }
          });
        },
      },
    },
  },
  preview: {
    allowedHosts: true,
  },
})
