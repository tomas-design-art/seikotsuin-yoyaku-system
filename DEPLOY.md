# 本番デプロイ手順

## 前提条件

- Docker + Docker Compose がインストール済み
- ドメインが取得済み（DNS の A レコードがサーバー IP を指している）
- SSL 証明書（Let's Encrypt 等）が用意できること

---

## 1. リポジトリ取得

```bash
git clone <repo-url> /opt/yoyaku-app
cd /opt/yoyaku-app
```

## 2. 環境変数ファイルの作成

```bash
cp .env.prod.example .env.prod
```

`.env.prod` を編集し、以下を必ず変更する:

| 変数 | 説明 | 生成方法 |
|------|------|---------|
| `POSTGRES_PASSWORD` | DB パスワード | `openssl rand -base64 24` |
| `SECRET_KEY` | JWT 署名キー | `openssl rand -hex 32` |
| `ADMIN_PASSWORD` | 管理画面パスワード | 任意の強固なパスワード |
| `GEMINI_API_KEY` | Google Gemini API キー | Google AI Studio で取得 |
| `CORS_ORIGINS` | フロントのドメイン | `https://yoyaku.example.com` |
| `CHATBOT_ALLOWED_ORIGINS` | チャットウィジェット埋込元 | カンマ区切りでドメイン指定 |
| `LINE_CHANNEL_SECRET` | LINE Messaging API | LINE Developers で取得 |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE チャネルトークン | LINE Developers で取得 |

`DATABASE_URL` のパスワード部分も `POSTGRES_PASSWORD` と一致させること。

## 3. SSL 証明書の配置

```bash
mkdir -p nginx/ssl
# Let's Encrypt の場合:
cp /etc/letsencrypt/live/yoyaku.example.com/fullchain.pem nginx/ssl/server.crt
cp /etc/letsencrypt/live/yoyaku.example.com/privkey.pem nginx/ssl/server.key
chmod 600 nginx/ssl/server.key
```

`nginx/nginx.conf` のドメイン名を実際のドメインに変更する。

## 4. ビルド & 起動

```bash
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
```

## 5. DB マイグレーション

```bash
docker compose -f docker-compose.prod.yml exec backend alembic upgrade head
```

## 6. 初期データ投入（初回のみ）

```bash
# 設定値（営業時間、管理者、チャットボット設定）
docker compose -f docker-compose.prod.yml exec backend python -m scripts.seed

# デモデータが必要な場合のみ（本番では通常不要）
# docker compose -f docker-compose.prod.yml exec backend python -m scripts.seed_demo
```

`seed.py` は冪等（同じデータが既に存在すれば skip）なので、複数回実行しても安全。

## 7. 動作確認

```bash
# コンテナ状態確認
docker compose -f docker-compose.prod.yml ps

# ログ確認
docker compose -f docker-compose.prod.yml logs -f backend

# ヘルスチェック
curl -k https://localhost/api/settings
```

- HTTPS でアクセスできるか
- ログインできるか
- 予約作成できるか
- 通知ログ削除ジョブが動くか

---

## 運用コマンド

### 再起動
```bash
docker compose -f docker-compose.prod.yml restart
```

### コード更新後の再デプロイ
```bash
git pull
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml exec backend alembic upgrade head
```

### DB バックアップ
```bash
docker compose -f docker-compose.prod.yml exec db pg_dump -U postgres reservation > backup_$(date +%Y%m%d).sql
```

### DB リストア
```bash
cat backup_YYYYMMDD.sql | docker compose -f docker-compose.prod.yml exec -T db psql -U postgres reservation
```

### ログ確認
```bash
docker compose -f docker-compose.prod.yml logs --tail=100 backend
docker compose -f docker-compose.prod.yml logs --tail=100 nginx
```

---

## アーキテクチャ概要

```
Internet
  │
  ▼
nginx (443/HTTPS) ──┬─ /api/*  → backend:8000 (FastAPI, Gunicorn 2workers)
                    └─ /*      → frontend:80  (nginx → 静的ファイル)
                    
backend → db:5432 (PostgreSQL 15)
backend → Gemini API (gemini-2.5-flash)
backend → LINE Messaging API
```

## セキュリティチェックリスト

- [ ] `SECRET_KEY` がデフォルト値でないこと
- [ ] `ADMIN_PASSWORD` がデフォルト値でないこと
- [ ] `POSTGRES_PASSWORD` が強固であること
- [ ] SSL 証明書が有効であること
- [ ] DB ポートが外部に公開されていないこと（127.0.0.1 のみ）
- [ ] `.env.prod` が `.gitignore` に含まれていること
- [ ] CORS_ORIGINS が必要最小限であること

---

## Render 固定手順（推奨）

### 1. Backend (Web Service)

- Runtime: Docker
- Root Directory: `backend`
- Dockerfile: `Dockerfile.prod`
- Health Check Path: `/health`

Render の Environment に以下を設定:

- `ENVIRONMENT=production`
- `DATABASE_URL`（Render PostgreSQL の Internal URL）
- `SECRET_KEY`（`openssl rand -hex 32`）
- `ADMIN_PASSWORD`（強い値）
- `CORS_ORIGINS=https://<frontend-domain>`
- `CHATBOT_ALLOWED_ORIGINS=https://<embed-site-domain>`
- `GEMINI_API_KEY`（利用時）
- `LINE_CHANNEL_SECRET` / `LINE_CHANNEL_ACCESS_TOKEN`（利用時）
- `MAIL_PROVIDER=icloud-imap`（HotPepperメール自動取得を使う場合、必須）
- `ICLOUD_EMAIL` / `ICLOUD_APP_PASSWORD`（**サービス移設時に忘れやすい。未設定だとポーリングが黙ってスキップされ続ける**）
- `IMAP_HOST` / `IMAP_PORT` / `IMAP_MAILBOX`（デフォルト値と異なる場合のみ）
- `HOTPEPPER_SENDER_FILTERS`（利用時）

### 2. Frontend (Static Site)

- Root Directory: `frontend`
- Build Command:

```bash
npm ci && npm run build && npm run build:widget && mkdir -p dist/chatbot && cp dist-widget/widget.js dist/chatbot/widget.js
```

- Publish Directory: `dist`

これで `https://<frontend-domain>/chatbot/widget.js` が配信されます。

### 3. 外部サイト埋め込みコード（固定）

```html
<script src="https://your-domain.com/chatbot/widget.js" data-api-base="https://your-domain.com/api/web_chatbot"></script>
```

`your-domain.com` は widget.js を配信するフロントドメインに置換してください。

### 4. CORS 設定の対応関係

- 管理画面/API利用元 → `CORS_ORIGINS`
- ウィジェット埋め込み元 → `CHATBOT_ALLOWED_ORIGINS`

この2つが一致していないと、埋め込みチャットから `/api/web_chatbot` 呼び出しがブラウザでブロックされます。