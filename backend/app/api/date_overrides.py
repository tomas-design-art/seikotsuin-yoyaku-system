"""個別日付オーバーライド API"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.date_override import DateOverride
from app.schemas.date_override import DateOverrideCreate, DateOverrideUpdate, DateOverrideResponse
from app.api.auth import require_admin
from app.services.notification_service import create_notification

router = APIRouter(prefix="/api/date-overrides", tags=["date-overrides"])


@router.get("/", response_model=list[DateOverrideResponse])
async def list_date_overrides(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(DateOverride).order_by(DateOverride.date))
    return result.scalars().all()


@router.post("/", response_model=DateOverrideResponse, dependencies=[Depends(require_admin)])
async def create_date_override(data: DateOverrideCreate, db: AsyncSession = Depends(get_db)):
    # 既存チェック
    existing = await db.execute(
        select(DateOverride).where(DateOverride.date == data.date)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="この日付のオーバーライドは既に存在します")

    override = DateOverride(
        date=data.date,
        is_open=data.is_open,
        open_time=data.open_time,
        close_time=data.close_time,
        label=data.label,
    )
    db.add(override)
    await create_notification(
        db, "date_override_updated",
        f"臨時営業設定登録: {data.date}",
    )
    await db.commit()
    await db.refresh(override)
    return override


@router.put("/{override_id}", response_model=DateOverrideResponse, dependencies=[Depends(require_admin)])
async def update_date_override(override_id: int, data: DateOverrideUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(DateOverride).where(DateOverride.id == override_id))
    override = result.scalar_one_or_none()
    if not override:
        raise HTTPException(status_code=404, detail="オーバーライドが見つかりません")

    override.is_open = data.is_open
    override.open_time = data.open_time
    override.close_time = data.close_time
    override.label = data.label
    await create_notification(
        db, "date_override_updated",
        f"臨時営業設定変更: {override.date}",
    )
    await db.commit()
    await db.refresh(override)
    return override


@router.delete("/{override_id}", dependencies=[Depends(require_admin)])
async def delete_date_override(override_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(DateOverride).where(DateOverride.id == override_id))
    override = result.scalar_one_or_none()
    if not override:
        raise HTTPException(status_code=404, detail="オーバーライドが見つかりません")

    await create_notification(
        db, "date_override_updated",
        f"臨時営業設定削除: {override.date}",
    )
    await db.delete(override)
    await db.commit()
    return {"ok": True}
