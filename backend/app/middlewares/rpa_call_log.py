"""/api/hotpepper/* への呼び出しを rpa_call_logs に記録するミドルウェア。

- 監査ログ (audit_logs) は触らない。完全に別系統。
- レスポンスが JSON 配列なら件数と先頭20件のID、dict なら "items"/"reservation_id" を拾う。
- 書き込みエラーは黙殺（観測機能が本処理を止めない）。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.database import async_session
from app.models.rpa_call_log import RpaCallLog

logger = logging.getLogger(__name__)

# 監視対象パス
_TARGET_PREFIX = "/api/hotpepper"
# 機微情報や巨大ボディが含まれる可能性のあるパスはボディサマリを取らない
_BODY_SUMMARY_BLOCKLIST = {"/api/hotpepper/parse-email"}


def _extract_count_and_ids(payload: Any) -> tuple[int | None, list[int] | None]:
    """JSON レスポンスから件数と ID 一覧を抽出。形式違いは None を返す。"""
    try:
        if isinstance(payload, list):
            ids = [
                int(item["id"])
                for item in payload
                if isinstance(item, dict) and "id" in item and isinstance(item.get("id"), int)
            ]
            return len(payload), ids[:20] if ids else None
        if isinstance(payload, dict):
            # /rpa-queue 形式: {items: [...]} or single mark-synced {reservation_id: 123}
            if "items" in payload and isinstance(payload["items"], list):
                ids = [
                    int(item["id"])
                    for item in payload["items"]
                    if isinstance(item, dict) and "id" in item and isinstance(item.get("id"), int)
                ]
                return len(payload["items"]), ids[:20] if ids else None
            if "reservation_id" in payload and isinstance(payload.get("reservation_id"), int):
                return 1, [int(payload["reservation_id"])]
            if "updated" in payload and isinstance(payload.get("updated"), int):
                ids = payload.get("ids") if isinstance(payload.get("ids"), list) else None
                return int(payload["updated"]), (ids[:20] if ids else None)
    except Exception:  # noqa: BLE001
        pass
    return None, None


class RpaCallLogMiddleware(BaseHTTPMiddleware):
    """RPA worker からの呼び出しを観測するミドルウェア。"""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith(_TARGET_PREFIX):
            return await call_next(request)

        start = time.perf_counter()
        # ボディ取得（POST等）。ストリーミング消費を避けるため body() で読み戻し可能化
        body_summary: dict | None = None
        if request.method in ("POST", "PUT", "PATCH") and path not in _BODY_SUMMARY_BLOCKLIST:
            try:
                raw = await request.body()
                if raw:
                    try:
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict):
                            # 長大なフィールドは値の型/長さのみ記録
                            summary: dict[str, Any] = {}
                            for k, v in list(parsed.items())[:10]:
                                if isinstance(v, (str, int, float, bool)) or v is None:
                                    if isinstance(v, str) and len(v) > 100:
                                        summary[k] = f"<str len={len(v)}>"
                                    else:
                                        summary[k] = v
                                elif isinstance(v, list):
                                    summary[k] = f"<list len={len(v)}>"
                                else:
                                    summary[k] = f"<{type(v).__name__}>"
                            body_summary = summary
                    except Exception:
                        body_summary = {"_raw_len": len(raw)}
            except Exception:  # noqa: BLE001
                body_summary = None

        response: Response = await call_next(request)

        duration_ms = int((time.perf_counter() - start) * 1000)

        # レスポンスボディをスニッフ（JSON のみ。ストリーミングは触らない）
        resp_count: int | None = None
        resp_ids: list[int] | None = None
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type and hasattr(response, "body_iterator"):
            try:
                body_chunks = []
                async for chunk in response.body_iterator:
                    body_chunks.append(chunk)
                body_bytes = b"".join(body_chunks)
                # 元のレスポンスを差し替えて返せるようにする
                from starlette.responses import Response as _Resp

                new_response = _Resp(
                    content=body_bytes,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    media_type=response.media_type,
                )
                try:
                    if body_bytes:
                        parsed = json.loads(body_bytes)
                        resp_count, resp_ids = _extract_count_and_ids(parsed)
                except Exception:  # noqa: BLE001
                    pass
                response = new_response
            except Exception as e:  # noqa: BLE001
                logger.debug("rpa_call_log body sniff failed: %s", e)

        # 書き込み（失敗しても黙殺）
        try:
            client_ip = request.client.host if request.client else None
            user_agent = request.headers.get("user-agent", "")[:300] or None
            query_params = dict(request.query_params) if request.query_params else None

            async with async_session() as db:
                log = RpaCallLog(
                    endpoint=path[:200],
                    method=request.method,
                    status_code=response.status_code,
                    query_params=query_params,
                    body_summary=body_summary,
                    response_count=resp_count,
                    response_ids=resp_ids,
                    duration_ms=duration_ms,
                    client_ip=client_ip,
                    user_agent=user_agent,
                )
                db.add(log)
                await db.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("rpa_call_log write failed path=%s err=%s", path, e)

        return response
