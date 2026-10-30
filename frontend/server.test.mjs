// server.js の確かめ（node --test）。画面サーバー経由の API の中継（オーナーの判断 2026-10-30）。
// 本物の API の代わりに、受け取った中身をそのまま返す小さなサーバーを立てて確かめる。
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { createApp, normalizeUpstream } from './server.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));

const BASIC = 'Basic ' + Buffer.from('staff:long-test-password').toString('base64');
const received = [];
let upstream;
let upstreamUrl;
let distDir;

function listen(server) {
    return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));
}

async function startScreenServer(extraEnv = {}) {
    const app = createApp({
        distDir,
        env: {
            BASIC_AUTH_USER: 'staff',
            BASIC_AUTH_PASSWORD: 'long-test-password',
            API_UPSTREAM_URL: upstreamUrl,
            INTERNAL_API_KEY: 'internal-test-key',
            ...extraEnv,
        },
    });
    const server = http.createServer(app);
    const port = await listen(server);
    return { server, base: `http://127.0.0.1:${port}` };
}

before(async () => {
    distDir = fs.mkdtempSync(path.join(os.tmpdir(), 'screen-dist-'));
    fs.writeFileSync(path.join(distDir, 'index.html'), '<!doctype html><title>screen</title>');

    upstream = http.createServer((req, res) => {
        let body = '';
        req.on('data', (chunk) => { body += chunk; });
        req.on('end', () => {
            received.push({ method: req.method, url: req.url, headers: req.headers, body });
            if (req.url.startsWith('/api/sse/events')) {
                res.writeHead(200, { 'content-type': 'text/event-stream' });
                res.write('data: one\n\n');
                setTimeout(() => res.end('data: two\n\n'), 30);
                return;
            }
            if (req.url.startsWith('/api/sse/quiet')) {
                // 本物の SSE と同じく、頭だけ先に返して、最初のお知らせはしばらく後
                res.writeHead(200, { 'content-type': 'text/event-stream' });
                res.flushHeaders();
                setTimeout(() => res.end('data: late\n\n'), 600);
                return;
            }
            const status = req.url.startsWith('/api/conflict') ? 409 : 200;
            res.writeHead(status, { 'content-type': 'application/json' });
            res.end(JSON.stringify({ ok: true }));
        });
    });
    upstreamUrl = `http://127.0.0.1:${await listen(upstream)}`;
});

after(() => {
    upstream.close();
    fs.rmSync(distDir, { recursive: true, force: true });
});

test('入口のログインなしでは、API も中継しない', async () => {
    const { server, base } = await startScreenServer();
    const countBefore = received.length;
    const res = await fetch(`${base}/api/reservations/`);
    server.close();
    assert.equal(res.status, 401);
    assert.equal(received.length, countBefore);
});

test('合言葉を付け、トークンは Authorization: Bearer に直して渡す。入口のログインの情報は渡さない', async () => {
    const { server, base } = await startScreenServer();
    const res = await fetch(`${base}/api/reservations/?start_date=2026-11-01`, {
        headers: { Authorization: BASIC, 'X-Auth-Token': 'staff-jwt' },
    });
    server.close();
    assert.equal(res.status, 200);
    const got = received.at(-1);
    assert.equal(got.url, '/api/reservations/?start_date=2026-11-01');
    assert.equal(got.headers['x-internal-key'], 'internal-test-key');
    assert.equal(got.headers['authorization'], 'Bearer staff-jwt');
    assert.equal(got.headers['x-auth-token'], undefined);
});

test('トークンが無いときは Authorization を付けない（Basic 認証の値を API に渡さない）', async () => {
    const { server, base } = await startScreenServer();
    await fetch(`${base}/api/settings/`, { headers: { Authorization: BASIC } });
    server.close();
    assert.equal(received.at(-1).headers['authorization'], undefined);
});

test('送り元が付けた合言葉や接続元は使わず、こちらの値に置き換える', async () => {
    const { server, base } = await startScreenServer();
    // 本番では Render が見た接続元を X-Forwarded-For の最後に書き足す（ここでは 198.51.100.5）。
    // 前の部分（203.0.113.99）は送り元が好きに書けるので、API には渡さない。
    await fetch(`${base}/api/auth/staff-login`, {
        method: 'POST',
        headers: {
            Authorization: BASIC,
            'X-Internal-Key': 'forged',
            'X-Forwarded-For': '203.0.113.99, 198.51.100.5',
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ pin: '0000' }),
    });
    server.close();
    const got = received.at(-1);
    assert.equal(got.headers['x-internal-key'], 'internal-test-key');
    assert.equal(got.headers['x-forwarded-for'], '198.51.100.5');
    assert.equal(got.method, 'POST');
    assert.deepEqual(JSON.parse(got.body), { pin: '0000' });
});

test('API の返事（状態の番号）はそのまま返す', async () => {
    const { server, base } = await startScreenServer();
    const res = await fetch(`${base}/api/conflict`, { headers: { Authorization: BASIC } });
    server.close();
    assert.equal(res.status, 409);
});

test('SSE は流れてきた順にそのまま返す', async () => {
    const { server, base } = await startScreenServer();
    const res = await fetch(`${base}/api/sse/events`, { headers: { Authorization: BASIC } });
    const text = await res.text();
    server.close();
    assert.equal(res.headers.get('content-type'), 'text/event-stream');
    assert.equal(text, 'data: one\n\ndata: two\n\n');
});

test('SSE は、最初のお知らせを待たずに応答の頭をすぐ返す（画面が「つながった」と分かるように）', async () => {
    const { server, base } = await startScreenServer();
    const started = Date.now();
    const res = await fetch(`${base}/api/sse/quiet`, { headers: { Authorization: BASIC } });
    const headersAfter = Date.now() - started;
    await res.text();
    server.close();
    assert.equal(res.status, 200);
    assert.ok(headersAfter < 400, `応答の頭が届くまで ${headersAfter}ms かかった`);
});

test('中継の設定が無ければ 503（API を素通しにしない）', async () => {
    const { server, base } = await startScreenServer({ INTERNAL_API_KEY: '' });
    const res = await fetch(`${base}/api/reservations/`, { headers: { Authorization: BASIC } });
    server.close();
    assert.equal(res.status, 503);
});

test('画面のファイルは今まで通り配る', async () => {
    const { server, base } = await startScreenServer();
    const res = await fetch(`${base}/timetable`, { headers: { Authorization: BASIC } });
    const text = await res.text();
    server.close();
    assert.equal(res.status, 200);
    assert.match(text, /<title>screen<\/title>/);
});

test('API の住所は、末尾の / や /api が付いていてもオリジンにそろえる', () => {
    assert.equal(normalizeUpstream('https://api.example.test/').href, 'https://api.example.test/');
    assert.equal(normalizeUpstream('https://api.example.test/api').href, 'https://api.example.test/');
    assert.equal(normalizeUpstream('https://api.example.test/api/').href, 'https://api.example.test/');
    assert.equal(normalizeUpstream(''), null);
});

async function freePort() {
    const probe = http.createServer();
    const port = await listen(probe);
    await new Promise((resolve) => probe.close(resolve));
    return port;
}

// 本番（Render）と同じく `node server.js` で起動して、画面を返すこと。
// 中継の設定がまだ無い（いまの本番）ときも、画面は今まで通り開ける。dist は `npm run build` で作られる
test('`node server.js` で起動し、中継の設定が無くても画面を返す', async () => {
    assert.ok(fs.existsSync(path.join(HERE, 'dist', 'index.html')), '先に npm run build を実行する');
    const port = await freePort();
    const child = spawn(process.execPath, ['server.js'], {
        cwd: HERE,
        env: { ...process.env, BASIC_AUTH_USER: 'staff', BASIC_AUTH_PASSWORD: 'long-test-password', PORT: String(port), API_UPSTREAM_URL: '', INTERNAL_API_KEY: '' },
        stdio: ['ignore', 'pipe', 'pipe'],
    });
    try {
        await new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error('10秒たっても起動しなかった')), 10000);
            child.stdout.on('data', (chunk) => {
                if (String(chunk).includes('listening')) {
                    clearTimeout(timer);
                    resolve();
                }
            });
            child.on('exit', (code) => reject(new Error(`起動せずに終わった（code ${code}）`)));
        });
        const page = await fetch(`http://127.0.0.1:${port}/`, { headers: { Authorization: BASIC } });
        assert.equal(page.status, 200);
        const locked = await fetch(`http://127.0.0.1:${port}/`);
        assert.equal(locked.status, 401);
    } finally {
        child.kill();
    }
});
