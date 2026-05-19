"""2段階認証API（スタッフPIN + 管理者ID/パスワード）"""
from datetime import datetime, timedelta
import hashlib
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from jose import jwt, JWTError
from passlib.context import CryptContext

from app.config import settings as app_settings
from app.database import get_db
from app.models.setting import Setting
from app.utils.datetime_jst import now_jst

router = APIRouter(prefix="/api/auth", tags=["auth"])

# --- crypto ---
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ALGORITHM = "HS256"
STAFF_TOKEN_HOURS = 24
ADMIN_TOKEN_HOURS = 8
STAFF_LOGIN_MAX_FAILURES = 5
STAFF_LOGIN_LOCK_MINUTES = 15


def _create_token(role: str, expires_delta: timedelta) -> str:
    expire = now_jst() + expires_delta
    payload = {"role": role, "exp": expire}
    return jwt.encode(payload, app_settings.secret_key, algorithm=ALGORITHM)


def _decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, app_settings.secret_key, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="トークンが無効または期限切れです")


# --- helpers ---
async def _get_setting(db: AsyncSession, key: str) -> str | None:
    result = await db.execute(select(Setting).where(Setting.key == key))
    row = result.scalar_one_or_none()
    return row.value if row else None


async def _set_setting(db: AsyncSession, key: str, value: str):
    result = await db.execute(select(Setting).where(Setting.key == key))
    row = result.scalar_one_or_none()
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))
    await db.flush()


async def _delete_setting(db: AsyncSession, key: str):
    result = await db.execute(select(Setting).where(Setting.key == key))
    row = result.scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.flush()


def _client_key(request: Request, x_forwarded_for: Optional[str], x_real_ip: Optional[str]) -> str:
    raw_ip = (x_forwarded_for or "").split(",")[0].strip()
    if not raw_ip:
        raw_ip = (x_real_ip or "").strip()
    if not raw_ip and request.client:
        raw_ip = request.client.host or "unknown"
    digest = hashlib.sha256(raw_ip.encode("utf-8")).hexdigest()[:24]
    return digest


async def _require_staff_login_not_locked(db: AsyncSession, client_key: str) -> None:
    raw_until = await _get_setting(db, f"staff_pin_lock_until:{client_key}")
    if not raw_until:
        return
    try:
        lock_until = datetime.fromisoformat(raw_until)
    except ValueError:
        await _delete_setting(db, f"staff_pin_lock_until:{client_key}")
        await db.commit()
        return
    if now_jst() < lock_until:
        raise HTTPException(status_code=429, detail="PIN入力の失敗が続いたため、一時的にロックされています")
    await _delete_setting(db, f"staff_pin_lock_until:{client_key}")
    await _delete_setting(db, f"staff_pin_failures:{client_key}")
    await db.commit()


async def _record_staff_login_failure(db: AsyncSession, client_key: str) -> None:
    key = f"staff_pin_failures:{client_key}"
    raw_count = await _get_setting(db, key)
    try:
        count = int(raw_count or "0") + 1
    except ValueError:
        count = 1

    if count >= STAFF_LOGIN_MAX_FAILURES:
        lock_until = now_jst() + timedelta(minutes=STAFF_LOGIN_LOCK_MINUTES)
        await _set_setting(db, f"staff_pin_lock_until:{client_key}", lock_until.isoformat())
        await _set_setting(db, key, str(count))
        await db.commit()
        raise HTTPException(status_code=429, detail="PIN入力の失敗が続いたため、一時的にロックしました")

    await _set_setting(db, key, str(count))
    await db.commit()


async def _clear_staff_login_failures(db: AsyncSession, client_key: str) -> None:
    await _delete_setting(db, f"staff_pin_failures:{client_key}")
    await _delete_setting(db, f"staff_pin_lock_until:{client_key}")
    await db.commit()


# --- schemas ---
class StaffLoginRequest(BaseModel):
    pin: str = Field(..., min_length=4, max_length=4, pattern=r"^\d{4}$")


class AdminLoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    token: str
    role: str


class ChangePasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=4)


# --- dependency functions (defined before endpoints that use them) ---
async def require_staff(authorization: Optional[str] = Header(None)) -> dict:
    """スタッフ以上の認証を要求"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="認証が必要です")
    payload = _decode_token(authorization[7:])
    if payload.get("role") not in ("staff", "admin"):
        raise HTTPException(status_code=403, detail="権限がありません")
    return payload


async def require_admin(authorization: Optional[str] = Header(None)) -> dict:
    """管理者認証を要求"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="管理者認証が必要です")
    payload = _decode_token(authorization[7:])
    if payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="管理者権限が必要です")
    return payload


# backward compat
async def verify_token(authorization: Optional[str] = Header(None)):
    return await require_staff(authorization)


# --- endpoints ---
@router.post("/staff-login", response_model=TokenResponse)
async def staff_login(
    body: StaffLoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_forwarded_for: Optional[str] = Header(None, alias="X-Forwarded-For"),
    x_real_ip: Optional[str] = Header(None, alias="X-Real-IP"),
):
    client_key = _client_key(request, x_forwarded_for, x_real_ip)
    await _require_staff_login_not_locked(db, client_key)

    stored_pin = await _get_setting(db, "staff_pin")
    if stored_pin is None:
        stored_pin = "1234"
    if body.pin != stored_pin:
        await _record_staff_login_failure(db, client_key)
        raise HTTPException(status_code=401, detail="PINが正しくありません")
    await _clear_staff_login_failures(db, client_key)
    token = _create_token("staff", timedelta(hours=STAFF_TOKEN_HOURS))
    return {"token": token, "role": "staff"}


@router.post("/admin-login", response_model=TokenResponse)
async def admin_login(body: AdminLoginRequest, db: AsyncSession = Depends(get_db)):
    stored_username = await _get_setting(db, "admin_username")
    stored_hash = await _get_setting(db, "admin_password_hash")
    if stored_username is None or stored_hash is None:
        raise HTTPException(status_code=500, detail="管理者アカウントが設定されていません")
    if body.username != stored_username:
        raise HTTPException(status_code=401, detail="IDまたはパスワードが正しくありません")
    if not pwd_context.verify(body.password, stored_hash):
        raise HTTPException(status_code=401, detail="IDまたはパスワードが正しくありません")
    token = _create_token("admin", timedelta(hours=ADMIN_TOKEN_HOURS))
    return {"token": token, "role": "admin"}


@router.post("/logout")
async def logout():
    return {"status": "ok"}


@router.get("/me")
async def me(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"authenticated": False, "role": None}
    token = authorization[7:]
    try:
        payload = _decode_token(token)
        return {"authenticated": True, "role": payload.get("role")}
    except HTTPException:
        return {"authenticated": False, "role": None}


@router.put("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    db: AsyncSession = Depends(get_db),
    _auth: dict = Depends(require_admin),
):
    hashed = pwd_context.hash(body.new_password)
    await _set_setting(db, "admin_password_hash", hashed)
    await db.commit()
    return {"status": "ok"}


@router.get("/verify")
async def verify(token: dict = Depends(require_staff)):
    return {"status": "authenticated", "role": token.get("role")}
