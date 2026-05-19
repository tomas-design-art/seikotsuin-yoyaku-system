# 接骨院予約管理システム

## 概要
接骨院の予約管理を紙ボードからデジタルに完全移行するためのWebアプリケーションです。

## 技術スタック
- **Frontend**: React + TypeScript + Vite + Tailwind CSS
- **Backend**: FastAPI + SQLAlchemy (async) + Alembic
- **Database**: PostgreSQL 15+
- **Container**: Docker + Docker Compose

## セットアップ

### 1. 環境変数
```bash
cp .env.example .env
# .envファイルを編集
```

### 2. Docker起動
```bash
docker-compose up --build
```

### 3. DBマイグレーション
```bash
docker-compose exec backend alembic upgrade head
```

### 4. 初期データ投入
```bash
docker-compose exec backend python scripts/seed.py
```

### 5. アクセス
- **フロントエンド**: http://localhost:5173
- **バックエンドAPI**: http://localhost:8000
- **API ドキュメント**: http://localhost:8000/docs

## 主な機能

### Phase 1（紙ボード完全置き換え）
- 5分刻みタイムテーブル（日表示/週表示）
- ドラッグ操作による予約登録
- 予約CRUD + 自動確定ロジック
- DB層でのEXCLUDE制約による二重予約完全防止
- ステータス遷移（確定/キャンセル申請/変更申請）
- SSEリアルタイム通知 + 通知音
- 施術者/メニュー/患者/設定管理

### Phase 2（外部チャネル連動）
- HotPepperメール自動解析→予約登録
- HotPepper枠押さえリマインド
- LINE予約メッセージ解析→予約提案

## テスト実行
```bash
cd backend
pip install -r requirements.txt
pytest tests/ -v
```

## 日計表エクスポート API

外部の日計表自動転記エージェント向けに、本日分の確定済み予約を取得できます。既存のスタッフ/管理者JWTを `Authorization: Bearer ...` で指定します。

```bash
curl -H "Authorization: Bearer <staff_or_admin_token>" \
	"http://localhost:8000/api/reservations/daily-report?date=2026-05-05&cutoff_time=2026-05-05T13:00:00%2B09:00"
```

Postman では `GET http://localhost:8000/api/reservations/daily-report` を作成し、Headers に `Authorization: Bearer <staff_or_admin_token>`、Query Params に `date=YYYY-MM-DD` と `cutoff_time=YYYY-MM-DDTHH:mm:ss+09:00` を設定してください。`date` 未指定時は本日JST、`cutoff_time` 未指定時は現在時刻JSTが使われます。レスポンスは `CONFIRMED` かつ指定日の `cutoff_time` 以前の予約だけを返し、キャンセル・仮予約・未来予約は含みません。
