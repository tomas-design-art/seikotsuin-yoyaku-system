"""通知管理サービス"""
import logging
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification_log import NotificationLog
from app.api.sse import broadcast_event

logger = logging.getLogger(__name__)


async def create_notification(
    db: AsyncSession,
    event_type: str,
    message: str,
    reservation_id: int | None = None,
    extra_data: dict | None = None,
):
    """通知を作成しSSEでブロードキャスト"""
    notif = NotificationLog(
        reservation_id=reservation_id,
        event_type=event_type,
        message=message,
    )
    db.add(notif)
    await db.flush()

    payload = {
        "id": notif.id,
        "reservation_id": reservation_id,
        "event_type": event_type,
        "message": message,
    }
    if extra_data:
        payload.update(extra_data)

    # ダーティリード/コミットタイミング競合ハック: 
    # API側の `db.commit()` とフロント側の超高速な再フェッチが衝突し、
    # 「自動更新が走ったのに、再読み込みしたデータがコミット前なので未反映」という時間差の競合（Race Condition）を防ぐため、
    # 0.15秒(150ms)だけ非同期遅延させてからブロードキャストする。
    async def _delayed_broadcast():
        import asyncio
        await asyncio.sleep(0.15)
        try:
            await broadcast_event(event_type, payload)
        except Exception as e:
            logger.error("Delayed broadcast failed: %s", str(e))

    import asyncio
    asyncio.create_task(_delayed_broadcast())

    return notif
