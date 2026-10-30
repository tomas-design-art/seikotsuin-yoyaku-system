"""API の入口の鍵（画面サーバー経由・オーナーの判断 2026-10-30）。

API を呼んでよいのは次の4つだけ。それ以外は、強さが enforce のとき 401 で断る。

- スタッフの画面を配るサーバー（frontend/server.js・入口のログインの後ろ）：X-Internal-Key。
  API の全部に通す（口ごとのログイン・暗証番号・管理者の確認は今まで通り別にかかる）。
- ホームページのサーバー：X-Homepage-Key。公開の口（/api/public/*）とホームページの予約だけ。
- HotPepper の RPA：Authorization: Bearer <RPA の合言葉>。RPA が使う3つの口だけ。
- 合言葉なし：死活確認と LINE の Webhook（署名で確かめる）だけ。

強さ（settings.api_access_mode）
- open：今まで通り全部通す（既定）。
- report：通すが、断るはずの呼び出しを記録する（切り替えの前に、合言葉の付け忘れを探す）。
- enforce：断る。

記録には、メソッドとパス（? より前）だけを残す。URL の後ろには電話番号などが入りうる。
"""
from __future__ import annotations

import hmac
import logging
import re
from typing import Any, Mapping

from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

_OPEN_PATHS = frozenset({"/", "/health", "/api/line/webhook"})
_HOMEPAGE_PATHS = frozenset({"/api/web_reserve"})
_HOMEPAGE_PREFIX = "/api/public/"
_RPA_PATHS = (
    re.compile(r"^/api/hotpepper/pending-sync$"),
    re.compile(r"^/api/hotpepper/reconcile-queue$"),
    re.compile(r"^/api/hotpepper/[^/]+/mark-synced$"),
)


def _same(given: str | None, expected: str) -> bool:
    # 合言葉が設定されていなければ、何を送られても開けない
    if not expected or given is None:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def _bearer(headers: Mapping[str, str]) -> str | None:
    value = headers.get("authorization") or ""
    if not value.startswith("Bearer "):
        return None
    return value[len("Bearer "):]


def decide(method: str, path: str, headers: Mapping[str, str], settings: Any) -> bool:
    """この呼び出しを通してよいか。headers のキーは小文字で渡す。"""
    path = path.rstrip("/") or "/"
    if path in _OPEN_PATHS:
        return True
    if _same(headers.get("x-internal-key"), settings.internal_api_key):
        return True
    if path in _HOMEPAGE_PATHS or path.startswith(_HOMEPAGE_PREFIX):
        if _same(headers.get("x-homepage-key"), settings.homepage_api_key):
            return True
    if any(pattern.match(path) for pattern in _RPA_PATHS):
        if _same(_bearer(headers), settings.rpa_api_key):
            return True
    return False


class ApiAccessMiddleware:
    """入口の鍵。SSE（流しっぱなしの応答）を妨げないよう、純粋な ASGI で書く。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from app.config import settings

        mode = settings.api_access_mode
        if mode == "open":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        path = scope.get("path", "")
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        if decide(method, path, headers, settings):
            await self.app(scope, receive, send)
            return

        if mode == "report":
            logger.warning("api_access would_reject method=%s path=%s", method, path)
            await self.app(scope, receive, send)
            return

        logger.warning("api_access rejected method=%s path=%s", method, path)
        response = JSONResponse({"detail": "認証が必要です"}, status_code=401)
        await response(scope, receive, send)


def warn_if_misconfigured(settings: Any) -> None:
    """本番で鍵がかかっていない・合言葉が無いときに、起動の記録に残す。"""
    if settings.environment != "production":
        return
    if settings.api_access_mode != "enforce":
        logger.warning("api_access mode=%s (本番は enforce にする)", settings.api_access_mode)
    if settings.api_access_mode != "open" and not settings.internal_api_key:
        logger.error("api_access: INTERNAL_API_KEY が未設定のため、スタッフの画面からの呼び出しも通らない")
