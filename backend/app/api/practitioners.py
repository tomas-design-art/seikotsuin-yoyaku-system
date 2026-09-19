from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update as sa_update

from app.database import get_db
from app.models.practitioner import Practitioner
from app.models.patient import Patient
from app.models.reservation import Reservation
from app.schemas.practitioner import PractitionerCreate, PractitionerUpdate, PractitionerResponse
from app.api.auth import require_admin
from pydantic import BaseModel

router = APIRouter(prefix="/api/practitioners", tags=["practitioners"])


@router.get("/", response_model=list[PractitionerResponse])
async def list_practitioners(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Practitioner).order_by(Practitioner.display_order, Practitioner.id)
    )
    return result.scalars().all()


@router.post("/", response_model=PractitionerResponse, status_code=201)
async def create_practitioner(data: PractitionerCreate, db: AsyncSession = Depends(get_db), _auth: dict = Depends(require_admin)):
    practitioner = Practitioner(**data.model_dump())
    db.add(practitioner)
    await db.commit()
    await db.refresh(practitioner)
    return practitioner


class PractitionerOrderItem(BaseModel):
    id: int
    display_order: int


@router.put("/reorder", response_model=list[PractitionerResponse])
async def reorder_practitioners(
    items: list[PractitionerOrderItem], db: AsyncSession = Depends(get_db), _auth: dict = Depends(require_admin)
):
    """並び順を保存する。タイムテーブルの列の順番と、指名なしのホットペッパー予約を
    どの先生から入れるか（同じ役割の中での順番）に効く。
    ※ /{practitioner_id} より前に置く（後ろだと "reorder" が id として解釈される）。
    """
    for item in items:
        await db.execute(
            sa_update(Practitioner).where(Practitioner.id == item.id).values(display_order=item.display_order)
        )
    await db.commit()
    result = await db.execute(select(Practitioner).order_by(Practitioner.display_order, Practitioner.id))
    return result.scalars().all()


@router.put("/{practitioner_id}", response_model=PractitionerResponse)
async def update_practitioner(
    practitioner_id: int, data: PractitionerUpdate, db: AsyncSession = Depends(get_db), _auth: dict = Depends(require_admin)
):
    result = await db.execute(select(Practitioner).where(Practitioner.id == practitioner_id))
    practitioner = result.scalar_one_or_none()
    if not practitioner:
        raise HTTPException(status_code=404, detail="施術者が見つかりません")
    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(practitioner, key, value)
    await db.commit()
    await db.refresh(practitioner)
    return practitioner


@router.delete("/{practitioner_id}", response_model=PractitionerResponse)
async def delete_practitioner(practitioner_id: int, db: AsyncSession = Depends(get_db), _auth: dict = Depends(require_admin)):
    """論理削除（is_active=False）"""
    result = await db.execute(select(Practitioner).where(Practitioner.id == practitioner_id))
    practitioner = result.scalar_one_or_none()
    if not practitioner:
        raise HTTPException(status_code=404, detail="施術者が見つかりません")
    practitioner.is_active = False
    await db.commit()
    await db.refresh(practitioner)
    return practitioner


@router.post("/{practitioner_id}/purge")
async def purge_practitioner(practitioner_id: int, db: AsyncSession = Depends(get_db), _auth: dict = Depends(require_admin)):
    """完全削除（2段階目）: 先に論理削除済みの施術者のみ削除可能。"""
    result = await db.execute(select(Practitioner).where(Practitioner.id == practitioner_id))
    practitioner = result.scalar_one_or_none()
    if not practitioner:
        raise HTTPException(status_code=404, detail="施術者が見つかりません")
    if practitioner.is_active:
        raise HTTPException(status_code=400, detail="先に施術者を無効化してください")

    # 予約は practitioner_id が必須のため、紐づく予約がある場合は完全削除不可
    reservation_exists = await db.execute(
        select(Reservation.id).where(Reservation.practitioner_id == practitioner_id).limit(1)
    )
    if reservation_exists.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=400,
            detail="この施術者に紐づく予約が存在するため完全削除できません。無効化のまま運用してください。",
        )

    # 患者の担当希望参照は解除
    await db.execute(
        sa_update(Patient)
        .where(Patient.preferred_practitioner_id == practitioner_id)
        .values(preferred_practitioner_id=None)
    )

    await db.delete(practitioner)
    await db.commit()
    return {"status": "ok", "deleted_id": practitioner_id}
