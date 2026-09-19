"""予約色設定API"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update as sa_update

from app.database import get_db
from app.models.reservation_color import ReservationColor, is_hotpepper_color_code
from app.models.reservation import Reservation
from app.schemas.reservation_color import (
    ReservationColorCreate,
    ReservationColorUpdate,
    ReservationColorResponse,
)
from app.api.auth import require_admin

router = APIRouter(prefix="/api/reservation-colors", tags=["reservation-colors"])

_HOTPEPPER_LOCKED = "ホットペッパー予約の色は、ホットペッパーからの予約を見分ける目印なので変更・削除できません"
_HOTPEPPER_RESERVED = "この色コードはホットペッパー予約専用です。別の色を選んでください"


@router.get("/", response_model=list[ReservationColorResponse])
async def list_colors(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ReservationColor).order_by(ReservationColor.display_order, ReservationColor.id)
    )
    return result.scalars().all()


@router.post("/", response_model=ReservationColorResponse, status_code=201)
async def create_color(data: ReservationColorCreate, db: AsyncSession = Depends(get_db), _auth: dict = Depends(require_admin)):
    if is_hotpepper_color_code(data.color_code):
        raise HTTPException(status_code=400, detail=_HOTPEPPER_RESERVED)
    if data.is_default:
        # 他のデフォルトを解除
        await db.execute(
            sa_update(ReservationColor).where(ReservationColor.is_default == True).values(is_default=False)
        )
    color = ReservationColor(**data.model_dump())
    db.add(color)
    await db.commit()
    await db.refresh(color)
    return color


@router.put("/{color_id}", response_model=ReservationColorResponse)
async def update_color(
    color_id: int, data: ReservationColorUpdate, db: AsyncSession = Depends(get_db), _auth: dict = Depends(require_admin)
):
    result = await db.execute(select(ReservationColor).where(ReservationColor.id == color_id))
    color = result.scalar_one_or_none()
    if not color:
        raise HTTPException(status_code=404, detail="色設定が見つかりません")
    if is_hotpepper_color_code(color.color_code):
        raise HTTPException(status_code=400, detail=_HOTPEPPER_LOCKED)

    update_data = data.model_dump(exclude_unset=True)
    if is_hotpepper_color_code(update_data.get("color_code")):
        raise HTTPException(status_code=400, detail=_HOTPEPPER_RESERVED)

    if update_data.get("is_default"):
        # 他のデフォルトを解除
        await db.execute(
            sa_update(ReservationColor)
            .where(ReservationColor.is_default == True, ReservationColor.id != color_id)
            .values(is_default=False)
        )

    for key, value in update_data.items():
        setattr(color, key, value)
    await db.commit()
    await db.refresh(color)
    return color


@router.delete("/{color_id}")
async def delete_color(color_id: int, db: AsyncSession = Depends(get_db), _auth: dict = Depends(require_admin)):
    result = await db.execute(select(ReservationColor).where(ReservationColor.id == color_id))
    color = result.scalar_one_or_none()
    if not color:
        raise HTTPException(status_code=404, detail="色設定が見つかりません")

    if is_hotpepper_color_code(color.color_code):
        raise HTTPException(status_code=400, detail=_HOTPEPPER_LOCKED)
    if color.is_default:
        raise HTTPException(status_code=400, detail="デフォルト色は削除できません")

    # この色を使っている予約のcolor_idをNULL（デフォルトにフォールバック）
    await db.execute(
        sa_update(Reservation).where(Reservation.color_id == color_id).values(color_id=None)
    )

    await db.delete(color)
    await db.commit()
    return {"status": "ok", "deleted_id": color_id}
