"""LINE管理者通知（Flex Message）ユーティリティ"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from app.config import settings
from app.services.line_reply import push_flex_message, push_message_with_access_token
from app.utils.datetime_jst import now_jst

logger = logging.getLogger(__name__)

_LAST_SOS_SENT: dict[str, float] = {}
_ACTIVE_INCIDENTS: dict[str, dict] = {}
_FEATURE_LABELS = {
    "hotpepper_poll": "HotPepperメール取得",
    "hotpepper_poll_job": "HotPepperポーリングジョブ",
    "startup_db_check": "起動時DB接続",
    "general": "システム全般",
}

_FIRST_ACTIONS = {
    "hotpepper_poll": "iCloud資格情報・IMAP接続・MAIL_PROVIDER設定を確認してください。",
    "hotpepper_poll_job": "バックエンドログで例外スタックを確認し、ジョブを再起動してください。",
    "startup_db_check": "DBコンテナ起動状態とDATABASE_URLのホスト名を確認してください。",
    "general": "直近のバックエンドログを確認し、再起動後に再発有無を確認してください。",
}

_HIGH_ERROR_TYPES = {
    "OperationalError",
    "DBAPIError",
    "InterfaceError",
    "ConnectionError",
    "ConnectError",
    "TimeoutError",
    "OSError",
    "gaierror",
}


def _truncate(text: str, limit: int = 800) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _resolve_feature_label(source: str | None) -> str:
    return _FEATURE_LABELS.get(source or "general", source or _FEATURE_LABELS["general"])


def _resolve_first_action(source: str | None) -> str:
    return _FIRST_ACTIONS.get(source or "general", _FIRST_ACTIONS["general"])


def _resolve_severity(
    *,
    source: str | None,
    error_type: str | None,
    failure_streak: int | None,
) -> str:
    # 起動時DB接続は可用性に直結するため常にHIGH
    if source == "startup_db_check":
        return "HIGH"

    if failure_streak is not None and failure_streak >= 3:
        return "HIGH"

    if error_type and error_type in _HIGH_ERROR_TYPES:
        return "HIGH"

    return "MEDIUM"


def _build_sos_message(
    *,
    title: str,
    detail: str | None,
    source: str | None,
    occurred_at: datetime,
    error_type: str | None = None,
    failure_streak: int | None = None,
) -> str:
    feature = _resolve_feature_label(source)
    action = _resolve_first_action(source)
    severity = _resolve_severity(source=source, error_type=error_type, failure_streak=failure_streak)
    incident_time = occurred_at.strftime("%Y-%m-%d %H:%M:%S JST")

    lines = [
        "[SOS] 予約システム異常通知",
        f"発生時刻: {incident_time}",
        f"重要度: {severity}",
        f"障害機能: {feature}",
        f"概要: {title}",
    ]

    if error_type:
        lines.append(f"例外種別: {error_type}")

    if failure_streak is not None:
        lines.append(f"連続失敗回数: {failure_streak}")

    if detail:
        lines.append(f"詳細: {_truncate(detail.strip(), 500)}")

    lines.append(f"一次対応: {action}")
    return "\n".join(lines)


def _build_recovered_message(
    *,
    source: str | None,
    title: str,
    started_at: datetime,
    recovered_at: datetime,
    latest_detail: str | None,
) -> str:
    feature = _resolve_feature_label(source)
    elapsed_seconds = int((recovered_at - started_at).total_seconds())
    elapsed_min, elapsed_sec = divmod(max(0, elapsed_seconds), 60)

    lines = [
        "[RECOVERED] 予約システム復旧通知",
        f"復旧時刻: {recovered_at.strftime('%Y-%m-%d %H:%M:%S JST')}",
        f"障害機能: {feature}",
        f"障害概要: {title}",
        f"停止時間: {elapsed_min}分{elapsed_sec}秒",
        "状態: 正常稼働に復帰しました",
    ]
    if latest_detail:
        lines.append(f"直近詳細: {_truncate(latest_detail.strip(), 300)}")
    return "\n".join(lines)


def build_hotpepper_failure_flex(reason: str, preview: str) -> dict:
    return {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "HotPepper手動確認", "weight": "bold", "size": "lg", "color": "#ffffff"}
            ],
            "backgroundColor": "#DC2626",
            "paddingAll": "12px",
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {"type": "text", "text": f"失敗理由: {reason}", "wrap": True, "size": "sm"},
                {"type": "separator", "margin": "sm"},
                {"type": "text", "text": preview[:280], "wrap": True, "size": "xs", "color": "#4B5563"},
            ],
        },
    }


async def push_admin_hotpepper_failure(reason: str, email_body: str) -> bool:
    if not settings.line_admin_user_id:
        logger.warning("LINE_ADMIN_USER_ID is empty; skip hotpepper failure push")
        return False
    preview = email_body.replace("\n", " ").strip()
    flex = build_hotpepper_failure_flex(reason, preview)
    return await push_flex_message(settings.line_admin_user_id, "HotPepper手動確認", flex)


async def push_developer_sos_alert(
    title: str,
    detail: str | None = None,
    *,
    source: str | None = None,
    error_type: str | None = None,
    failure_streak: int | None = None,
    dedupe_key: str | None = None,
    min_interval_seconds: int = 300,
) -> bool:
    """開発者向けの緊急SOS通知をLINEへ送る。"""
    target_user_id = settings.admin_line_developer_user_id
    if not target_user_id:
        logger.warning("ADMIN_LINE_DEVELOPER_USER_ID is empty; skip SOS push")
        return False

    developer_token = settings.line_channel_developer_access_token
    if not developer_token:
        logger.warning("LINE_CHANNEL_DEVELOPER_ACCESS_TOKEN is empty; skip SOS push")
        return False

    key = dedupe_key or f"{source or 'general'}:{title}"
    now_ts = time.time()
    last_ts = _LAST_SOS_SENT.get(key)
    if last_ts and (now_ts - last_ts) < min_interval_seconds:
        logger.info("SOS push deduped: key=%s", key)
        return False

    message = _build_sos_message(
        title=title,
        detail=detail,
        source=source,
        occurred_at=now_jst(),
        error_type=error_type,
        failure_streak=failure_streak,
    )

    ok = await push_message_with_access_token(target_user_id, message, developer_token)
    if ok:
        _LAST_SOS_SENT[key] = now_ts
        _ACTIVE_INCIDENTS[key] = {
            "source": source,
            "title": title,
            "started_at": now_jst(),
            "latest_detail": detail,
        }
    return ok


async def push_developer_recovered_alert(
    *,
    dedupe_key: str,
    title: str | None = None,
    source: str | None = None,
    latest_detail: str | None = None,
) -> bool:
    """開発者向け復旧通知（SOSと同じ宛先）。"""
    target_user_id = settings.admin_line_developer_user_id
    if not target_user_id:
        logger.warning("ADMIN_LINE_DEVELOPER_USER_ID is empty; skip RECOVERED push")
        return False

    developer_token = settings.line_channel_developer_access_token
    if not developer_token:
        logger.warning("LINE_CHANNEL_DEVELOPER_ACCESS_TOKEN is empty; skip RECOVERED push")
        return False

    incident = _ACTIVE_INCIDENTS.get(dedupe_key)
    if not incident:
        return False

    resolved_title = title or incident.get("title") or "システム異常"
    resolved_source = source or incident.get("source")
    resolved_detail = latest_detail or incident.get("latest_detail")
    started_at = incident.get("started_at") or now_jst()
    recovered_at = now_jst()

    message = _build_recovered_message(
        source=resolved_source,
        title=resolved_title,
        started_at=started_at,
        recovered_at=recovered_at,
        latest_detail=resolved_detail,
    )

    ok = await push_message_with_access_token(target_user_id, message, developer_token)
    if ok:
        _ACTIVE_INCIDENTS.pop(dedupe_key, None)
        _LAST_SOS_SENT.pop(dedupe_key, None)
    return ok
