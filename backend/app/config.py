import os
import secrets
import logging
from pathlib import Path
from typing import Literal
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

_INSECURE_DEFAULTS = {
    "dev-secret-key",
    "your-secret-key",
    "changeme",
    "CHANGE_ME_GENERATE_WITH_OPENSSL",
    "",
}

# backend/.env → プロジェクトルート/.env の順で探す
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
if not _ENV_FILE.exists():
    _ENV_FILE = Path(".env")


class Settings(BaseSettings):
    app_title: str = "予約管理システム"  # 院名やシステム名（テンプレ複製時に変更）
    database_url: str = "postgresql+asyncpg://postgres:postgres@db:5432/reservation?ssl=disable"
    secret_key: str = "dev-secret-key"
    admin_password: str = "admin123"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.1-flash-lite"
    line_channel_secret: str = ""
    line_channel_access_token: str = ""
    line_channel_developer_access_token: str = ""
    line_admin_user_id: str = ""
    admin_line_developer_user_id: str = ""
    mail_provider: str = "gmail"
    icloud_email: str = ""
    icloud_app_password: str = ""
    imap_host: str = "imap.mail.me.com"
    imap_port: int = 993
    imap_mailbox: str = "INBOX"
    hotpepper_sender_filters: str = "SALON BOARD <yoyaku_system@salonboard.com>"
    hotpepper_poll_interval_minutes: int = 5
    hotpepper_poll_fetch_limit: int = 50
    hotpepper_poll_max_retries: int = 3
    hotpepper_poll_retry_base_seconds: int = 2
    hotpepper_poll_search_days: int = 1  # IMAP検索対象の過去N日分（既読/未読問わず）
    rpa_horizon_days: int = 90  # RPA転記対象の上限日数（SalonBoardカレンダー上限）
    notification_retention_days: int = 30
    notification_unread_retention_days: int = 30
    chatbot_allowed_origins: str = ""
    cors_origins: str = ""  # カンマ区切りで追加のCORSオリジンを指定
    environment: str = "development"  # development | production
    line_autopilot_enabled: bool = False  # True: setup完了済み患者だけLINE自動応答を許可
    line_inbox_rollout: Literal["legacy", "autopilot", "all"] = "legacy"
    # API の入口の鍵（app/middlewares/api_access.py）。open＝今まで通り／report＝通すが記録／enforce＝断る
    api_access_mode: Literal["open", "report", "enforce"] = "open"
    internal_api_key: str = ""  # スタッフの画面を配るサーバー（frontend/server.js）が付ける合言葉
    homepage_api_key: str = ""  # ホームページのサーバーが付ける合言葉
    rpa_api_key: str = ""  # HotPepper の RPA が Authorization: Bearer で付ける合言葉
    daily_report_api_key: str = ""  # 日計表のツールが Authorization: Bearer で付ける合言葉

    class Config:
        env_file = str(_ENV_FILE)
        extra = "ignore"


settings = Settings()

# --- 本番環境での安全性チェック ---
if settings.environment == "production":
    if settings.secret_key in _INSECURE_DEFAULTS:
        raise RuntimeError(
            "SECRET_KEY が未設定またはデフォルト値です。"
            "本番環境では `openssl rand -hex 32` 等で生成した値を設定してください。"
        )
    if settings.admin_password in {"admin123", "password", ""}:
        raise RuntimeError(
            "ADMIN_PASSWORD が未設定またはデフォルト値です。"
            "本番環境では十分に強い値を設定してください。"
        )
elif settings.secret_key in _INSECURE_DEFAULTS:
    logger.warning("SECRET_KEY がデフォルト値です。本番デプロイ前に変更してください。")
