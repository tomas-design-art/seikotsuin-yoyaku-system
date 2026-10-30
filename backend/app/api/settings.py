from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.database import get_db
from app.models.setting import Setting
from app.models.patient import Patient
from app.models.reservation import Reservation
from app.schemas.setting import SettingUpdate, SettingResponse
from app.api.auth import require_admin

router = APIRouter(prefix="/api/settings", tags=["settings"])

_INTERNAL_PREFIXES = (
    "staff_pin_failures:",
    "staff_pin_lock_until:",
    "admin_login_failures:",
    "admin_login_lock_until:",
)
# 管理者の ID とパスワードは専用の口（/api/auth/*）だけで扱う。一覧にも出さず、ここから書き換えさせない
_AUTH_KEYS = frozenset({"admin_username", "admin_password_hash"})
# 欄は残す（管理者が書き換えるため）が、値は返さない
_MASKED_KEYS = frozenset({"staff_pin"})


def _is_visible_setting(key: str) -> bool:
    return not key.startswith(_INTERNAL_PREFIXES) and key not in _AUTH_KEYS


def _response(setting: Setting) -> SettingResponse:
    value = "" if setting.key in _MASKED_KEYS else setting.value
    return SettingResponse(id=setting.id, key=setting.key, value=value, updated_at=setting.updated_at)


@router.get("/", response_model=list[SettingResponse])
async def list_settings(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Setting).order_by(Setting.key))
    return [_response(s) for s in result.scalars().all() if _is_visible_setting(s.key)]


@router.put("/{key}", response_model=SettingResponse)
async def update_setting(key: str, data: SettingUpdate, db: AsyncSession = Depends(get_db), _auth: dict = Depends(require_admin)):
    if not _is_visible_setting(key):
        raise HTTPException(status_code=404, detail=f"設定キー '{key}' が見つかりません")
    if key == "staff_pin" and (not data.value.isdigit() or len(data.value) != 4):
        raise HTTPException(status_code=400, detail="スタッフPINは4桁の数字で設定してください")
    result = await db.execute(select(Setting).where(Setting.key == key))
    setting = result.scalar_one_or_none()
    if not setting:
        raise HTTPException(status_code=404, detail=f"設定キー '{key}' が見つかりません")
    setting.value = data.value
    await db.commit()
    await db.refresh(setting)
    return _response(setting)


@router.post("/reset-operational-data", tags=["settings"])
async def reset_operational_data(
    db: AsyncSession = Depends(get_db),
    _auth: dict = Depends(require_admin),
):
    """本番導入前の初期化: 患者データと全予約を削除する。
    施術者・メニュー・スケジュール・設定・予約色は保持する。
    """
    # 予約を先に削除（patients への FK があるため）
    res_result = await db.execute(delete(Reservation))
    deleted_reservations = res_result.rowcount

    # 患者を削除
    pat_result = await db.execute(delete(Patient))
    deleted_patients = pat_result.rowcount

    await db.commit()
    return {
        "status": "ok",
        "deleted_reservations": deleted_reservations,
        "deleted_patients": deleted_patients,
    }
