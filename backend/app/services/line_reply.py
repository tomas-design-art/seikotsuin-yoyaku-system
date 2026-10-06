"""LINE返信サービス"""
import logging
import httpx
from typing import Optional

from app.config import settings
from app.services import autopilot_log

logger = logging.getLogger(__name__)


async def _post_line_reply(reply_token: str, messages: list[dict]) -> bool:
    ok = await _send_line_reply(reply_token, messages)
    autopilot_log.note_send(via="reply", to=None, messages=messages, ok=ok)
    return ok


async def _send_line_reply(reply_token: str, messages: list[dict]) -> bool:
    if not settings.line_channel_access_token or settings.line_channel_access_token == "xxx":
        logger.warning("LINE access token not configured, skipping reply")
        return False

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.line.me/v2/bot/message/reply",
                headers={
                    "Authorization": f"Bearer {settings.line_channel_access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "replyToken": reply_token,
                    "messages": messages,
                },
                timeout=10,
            )
            if response.status_code == 200:
                logger.info("LINE reply sent successfully")
                return True
            logger.error(f"LINE reply failed: {response.status_code} {response.text}")
            return False
    except Exception as e:
        logger.error(f"LINE reply error: {e}")
        return False


async def reply_to_line(reply_token: str, message: str) -> bool:
    """LINE Messaging APIで返信"""
    return await _post_line_reply(reply_token, [{"type": "text", "text": message}])


async def reply_text_with_quick_reply(reply_token: str, message: str, items: list[dict]) -> bool:
    """LINE QuickReply付きテキスト返信"""
    payload = {
        "type": "text",
        "text": message,
        "quickReply": {
            "items": items,
        },
    }
    return await _post_line_reply(reply_token, [payload])


async def push_message(user_id: str, message: str) -> bool:
    """LINEプッシュメッセージ"""
    return await push_message_with_access_token(user_id, message, settings.line_channel_access_token)


async def push_text_with_quick_reply(user_id: str, message: str, items: list[dict]) -> bool:
    """LINE QuickReply付きプッシュメッセージ"""
    ok = await _send_push_text_with_quick_reply(user_id, message, items)
    autopilot_log.note_send(
        via="push",
        to=user_id,
        messages=[{"type": "text", "text": message, "quickReply": {"items": items}}],
        ok=ok,
    )
    return ok


async def _send_push_text_with_quick_reply(user_id: str, message: str, items: list[dict]) -> bool:
    if not settings.line_channel_access_token or settings.line_channel_access_token == "xxx":
        logger.warning("LINE access token not configured, skipping push")
        return False

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.line.me/v2/bot/message/push",
                headers={
                    "Authorization": f"Bearer {settings.line_channel_access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "to": user_id,
                    "messages": [
                        {
                            "type": "text",
                            "text": message,
                            "quickReply": {"items": items},
                        }
                    ],
                },
                timeout=10,
            )
            return response.status_code == 200
    except Exception as e:
        logger.error(f"LINE push error: {e}")
        return False


async def push_message_with_access_token(user_id: str, message: str, access_token: str | None) -> bool:
    """任意トークンでLINEプッシュメッセージ"""
    ok = await _send_push_message_with_access_token(user_id, message, access_token)
    autopilot_log.note_send(via="push", to=user_id, messages=[{"type": "text", "text": message}], ok=ok)
    return ok


async def _send_push_message_with_access_token(user_id: str, message: str, access_token: str | None) -> bool:
    if not access_token or access_token == "xxx":
        logger.warning("LINE access token not configured, skipping push")
        return False

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.line.me/v2/bot/message/push",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "to": user_id,
                    "messages": [
                        {"type": "text", "text": message}
                    ],
                },
                timeout=10,
            )
            return response.status_code == 200
    except Exception as e:
        logger.error(f"LINE push error: {e}")
        return False


async def reply_flex_message(reply_token: str, alt_text: str, contents: dict) -> bool:
    """LINE Messaging APIでFlex返信"""
    return await _post_line_reply(
        reply_token,
        [
            {
                "type": "flex",
                "altText": alt_text,
                "contents": contents,
            }
        ],
    )


async def push_flex_message(user_id: str, alt_text: str, contents: dict) -> bool:
    """LINE Messaging APIでFlexプッシュ"""
    ok = await _send_push_flex_message(user_id, alt_text, contents)
    autopilot_log.note_send(
        via="push",
        to=user_id,
        messages=[{"type": "flex", "altText": alt_text, "contents": contents}],
        ok=ok,
    )
    return ok


async def _send_push_flex_message(user_id: str, alt_text: str, contents: dict) -> bool:
    if not settings.line_channel_access_token or settings.line_channel_access_token == "xxx":
        logger.warning("LINE access token not configured, skipping flex push")
        return False

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.line.me/v2/bot/message/push",
                headers={
                    "Authorization": f"Bearer {settings.line_channel_access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "to": user_id,
                    "messages": [
                        {
                            "type": "flex",
                            "altText": alt_text,
                            "contents": contents,
                        }
                    ],
                },
                timeout=10,
            )
            if response.status_code == 200:
                logger.info("LINE flex push sent successfully")
                return True
            logger.error(f"LINE flex push failed: {response.status_code} {response.text}")
            return False
    except Exception as e:
        logger.error(f"LINE flex push error: {e}")
        return False
