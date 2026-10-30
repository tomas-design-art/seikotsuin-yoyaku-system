// Vite ビルド成果物 (dist) を Basic 認証付きで配信する本番サーバー
// Render の Web Service (Node) で起動する想定
//
// 必須環境変数:
//   BASIC_AUTH_USER
//   BASIC_AUTH_PASSWORD
// API の中継（画面サーバー経由・オーナーの判断 2026-10-30）:
//   API_UPSTREAM_URL  (API のオリジン。例 https://api.example.com)
//   INTERNAL_API_KEY  (API へ渡す合言葉。API 側の INTERNAL_API_KEY と同じ値)
//   どちらかが無いと /api/* は 503 を返す。画面は VITE_API_URL を /api にしてビルドする。
// 任意環境変数:
//   PORT           (Render が自動設定。ローカルは 3000)
//   AUTH_REALM     (WWW-Authenticate realm 文字列)
//   SKIP_AUTH_PATHS(カンマ区切り。ヘルスチェック等を除外したい場合のみ)

import express from 'express';
import http from 'node:http';
import https from 'node:https';
import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// 定数時間比較（タイミング攻撃対策）
function safeEqual(a, b) {
    if (typeof a !== 'string' || typeof b !== 'string') return false;
    if (a.length !== b.length) return false;
    let result = 0;
    for (let i = 0; i < a.length; i++) {
        result |= a.charCodeAt(i) ^ b.charCodeAt(i);
    }
    return result === 0;
}

function createBasicAuth({ expectedUser, expectedPass, realm, skipAuthPaths }) {
    const authConfigured =
        typeof expectedUser === 'string' &&
        expectedUser.length > 0 &&
        typeof expectedPass === 'string' &&
        expectedPass.length > 0;

    function sendUnauthorized(res) {
        res.set('WWW-Authenticate', `Basic realm="${realm}", charset="UTF-8"`);
        res.set('Cache-Control', 'no-store');
        res.status(401).type('text/plain').send('Authentication required');
    }

    return function basicAuth(req, res, next) {
        // 除外パス（例: /healthz）
        if (skipAuthPaths.includes(req.path)) {
            return next();
        }

        // Fail closed: 環境変数が無ければ全拒否
        if (!authConfigured) {
            res.set('Cache-Control', 'no-store');
            return res.status(503).type('text/plain').send('Service unavailable: auth not configured');
        }

        const header = req.headers['authorization'] || '';
        if (typeof header !== 'string' || !header.toLowerCase().startsWith('basic ')) {
            return sendUnauthorized(res);
        }

        const encoded = header.slice(6).trim();
        let decoded;
        try {
            decoded = Buffer.from(encoded, 'base64').toString('utf-8');
        } catch {
            return sendUnauthorized(res);
        }

        const sepIdx = decoded.indexOf(':');
        if (sepIdx < 0) return sendUnauthorized(res);

        const user = decoded.slice(0, sepIdx);
        const pass = decoded.slice(sepIdx + 1);

        const userOk = safeEqual(user, expectedUser);
        const passOk = safeEqual(pass, expectedPass);
        if (!userOk || !passOk) {
            return sendUnauthorized(res);
        }

        return next();
    };
}

// API のオリジンにそろえる（末尾の / や /api は付いていてもよい）
export function normalizeUpstream(raw) {
    if (!raw) return null;
    const url = new URL(raw);
    url.pathname = url.pathname.replace(/\/+$/, '').replace(/\/api$/, '');
    url.search = '';
    url.hash = '';
    return url;
}

// API へ渡さないヘッダー：入口のログインの情報、接続の制御、送り元に書き換えさせないもの
const DROP_REQUEST_HEADERS = new Set([
    'host',
    'connection',
    'keep-alive',
    'proxy-authorization',
    'te',
    'upgrade',
    'authorization',
    'cookie',
    'x-auth-token',
    'x-internal-key',
    'x-forwarded-for',
    'x-forwarded-host',
    'x-forwarded-proto',
    'x-real-ip',
]);
const DROP_RESPONSE_HEADERS = new Set(['connection', 'keep-alive', 'transfer-encoding']);

function createApiProxy({ upstream, internalKey }) {
    return function apiProxy(req, res) {
        if (!upstream || !internalKey) {
            res.set('Cache-Control', 'no-store');
            return res.status(503).json({ detail: 'API の中継が設定されていません' });
        }

        // 文字列でつなぐ（URL の相対解決だと「//api/...」が別のホストと読まれるため）
        const target = new URL(upstream.origin + upstream.pathname.replace(/\/+$/, '') + req.originalUrl);
        const headers = {};
        for (const [name, value] of Object.entries(req.headers)) {
            if (!DROP_REQUEST_HEADERS.has(name)) headers[name] = value;
        }
        // 画面はトークンを X-Auth-Token で送る（Authorization は入口のログインが使うため）
        const token = req.headers['x-auth-token'];
        if (typeof token === 'string' && token) {
            headers['authorization'] = `Bearer ${token}`;
        }
        headers['x-internal-key'] = internalKey;
        // PIN などの失敗の数え方に使う。送り元が付けた値は捨て、ここで見えた接続元だけを渡す
        headers['x-forwarded-for'] = req.ip || '';
        headers['host'] = target.host;

        const client = target.protocol === 'https:' ? https : http;
        const upstreamReq = client.request(target, { method: req.method, headers }, (upstreamRes) => {
            const outHeaders = {};
            for (const [name, value] of Object.entries(upstreamRes.headers)) {
                if (!DROP_RESPONSE_HEADERS.has(name)) outHeaders[name] = value;
            }
            res.writeHead(upstreamRes.statusCode || 502, outHeaders);
            // 応答の頭はすぐ送る。SSE は最初のお知らせまで本文が無いので、待つと画面がつながらない
            res.flushHeaders();
            // SSE（/api/sse/events）も、届いた分からそのまま流す
            upstreamRes.pipe(res);
        });
        upstreamReq.on('error', () => {
            if (!res.headersSent) {
                res.set('Cache-Control', 'no-store');
                res.status(502).json({ detail: 'API に接続できませんでした' });
            } else {
                res.end();
            }
        });
        // 画面が閉じられたら API への接続も切る（SSE がつながったまま残らないように）
        res.on('close', () => upstreamReq.destroy());
        req.pipe(upstreamReq);
    };
}

export function createApp({ distDir, env }) {
    const app = express();
    app.disable('x-powered-by');
    app.set('trust proxy', 1); // Render の HTTPS 終端配下で動作

    // すべてのリクエストに Basic 認証を適用（/api の中継も、この後ろ）
    app.use(
        createBasicAuth({
            expectedUser: env.BASIC_AUTH_USER,
            expectedPass: env.BASIC_AUTH_PASSWORD,
            realm: env.AUTH_REALM || 'Staff Only',
            skipAuthPaths: (env.SKIP_AUTH_PATHS || '')
                .split(',')
                .map((s) => s.trim())
                .filter(Boolean),
        }),
    );

    // API の中継。本文は読まずにそのまま流す（express.json などは使わない）
    app.use(
        '/api',
        createApiProxy({
            upstream: normalizeUpstream(env.API_UPSTREAM_URL),
            internalKey: env.INTERNAL_API_KEY || '',
        }),
    );

    // 静的ファイル配信
    app.use(
        express.static(distDir, {
            index: false,
            maxAge: '1h',
            setHeaders: (res, filePath) => {
                // 認証後のレスポンスを共有キャッシュに載せない
                res.setHeader('Cache-Control', 'private, max-age=0, must-revalidate');
                if (filePath.endsWith('.html')) {
                    res.setHeader('Cache-Control', 'no-store');
                }
            },
        }),
    );

    // SPA フォールバック: 未マッチの GET はすべて index.html を返す
    app.get('*', (_req, res) => {
        res.set('Cache-Control', 'no-store');
        res.sendFile(path.join(distDir, 'index.html'));
    });

    return app;
}

function main() {
    const DIST_DIR = path.join(__dirname, 'dist');
    const PORT = Number(process.env.PORT) || 3000;

    // 起動時チェック: dist が存在しなければ即エラー終了
    if (!fs.existsSync(DIST_DIR) || !fs.existsSync(path.join(DIST_DIR, 'index.html'))) {
        console.error(`[server] dist not found at ${DIST_DIR}. Run "npm run build" first.`);
        process.exit(1);
    }

    const authConfigured = Boolean(process.env.BASIC_AUTH_USER && process.env.BASIC_AUTH_PASSWORD);
    if (!authConfigured) {
        console.error(
            '[server] BASIC_AUTH_USER / BASIC_AUTH_PASSWORD are not set. ' +
            'All requests will be denied (fail closed).',
        );
    }
    const proxyConfigured = Boolean(process.env.API_UPSTREAM_URL && process.env.INTERNAL_API_KEY);

    const app = createApp({ distDir: DIST_DIR, env: process.env });
    app.listen(PORT, '0.0.0.0', () => {
        console.log(`[server] listening on 0.0.0.0:${PORT}`);
        console.log(`[server] serving ${DIST_DIR}`);
        console.log(`[server] auth configured: ${authConfigured}`);
        console.log(`[server] api proxy configured: ${proxyConfigured}`);
    });
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
    main();
}
