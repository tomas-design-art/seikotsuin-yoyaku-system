from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, and_, update
from typing import Literal
import re

from app.database import get_db
from app.api.auth import require_staff
from app.models.patient import Patient
from app.models.reservation import Reservation
from app.schemas.patient import (
    PatientCreate, PatientUpdate, PatientResponse, PatientPageResponse,
    CandidateQuery, CandidateResponse, PatientPurgeRequest, _normalize_phone, build_name,
)
from app.utils.normalize import normalize_search_text, HIRA_CHARS, KATA_CHARS

router = APIRouter(prefix="/api/patients", tags=["patients"])


def _normalize_for_compare(s: str | None) -> str:
    """比較用の正規化文字列"""
    if not s:
        return ""
    s = s.replace("\u3000", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


def _sql_kana_normalize(col):
    """SQLカラムのひらがな→カタカナ変換 + lower (PostgreSQL translate)"""
    return func.lower(func.translate(col, HIRA_CHARS, KATA_CHARS))


async def _generate_patient_number(db: AsyncSession) -> str:
    """P000001 形式で一意な患者番号を自動採番"""
    result = await db.execute(
        select(func.max(Patient.patient_number))
        .where(Patient.patient_number.op("~")(r"^P\d+$"))
    )
    max_num = result.scalar()
    if max_num:
        next_val = int(max_num[1:]) + 1
    else:
        next_val = 1
    return f"P{next_val:06d}"


@router.get("/search", response_model=list[PatientResponse])
async def search_patients(
    q: str = Query(..., min_length=1),
    include_inactive: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    _auth: dict = Depends(require_staff),
):
    """名前・読み方・診察券番号・電話番号で部分一致検索(active only)

    ひらがな/カタカナ/半角カナを正規化して検索するため、
    「やまだ」「ヤマダ」「ﾔﾏﾀﾞ」のいずれでもヒットする。
    """
    normalized_q = _normalize_for_compare(q)
    kana_q = normalize_search_text(q)  # ひらがな→カタカナ, 半角→全角 統一
    phone_q = _normalize_phone(q) or q
    active_filter = True if include_inactive else Patient.is_active == True
    result = await db.execute(
        select(Patient).where(
            and_(
                active_filter,
                or_(
                    Patient.name.ilike(f"%{normalized_q}%"),
                    Patient.last_name.ilike(f"%{normalized_q}%"),
                    Patient.first_name.ilike(f"%{normalized_q}%"),
                    Patient.reading.ilike(f"%{normalized_q}%"),
                    Patient.last_name_kana.ilike(f"%{normalized_q}%"),
                    Patient.first_name_kana.ilike(f"%{normalized_q}%"),
                    # カナ正規化検索: ひらがな⇔カタカナを統一して比較
                    _sql_kana_normalize(Patient.reading).contains(kana_q),
                    _sql_kana_normalize(Patient.last_name_kana).contains(kana_q),
                    _sql_kana_normalize(Patient.first_name_kana).contains(kana_q),
                    Patient.patient_number.ilike(f"%{q}%"),
                    Patient.phone.ilike(f"%{phone_q}%"),
                ),
            )
        ).order_by(Patient.name).limit(50)
    )
    return result.scalars().all()


@router.post("/candidates", response_model=list[CandidateResponse])
async def find_candidates(data: CandidateQuery, db: AsyncSession = Depends(get_db), _auth: dict = Depends(require_staff)):
    """既存患者候補を検索 — 広範囲マッチで重複を防止"""
    # 検索用の名前を組み立て
    if data.registration_mode == "full_name":
        query_name = _normalize_for_compare(data.full_name)
    else:
        query_name = _normalize_for_compare(
            f"{data.last_name or ''} {data.first_name or ''}".strip()
        )
    query_name_nospace = query_name.replace(" ", "")

    if not query_name_nospace:
        return []

    phone_q = _normalize_phone(data.phone) if data.phone else None

    # 氏名完全一致で検索（同姓同名のみ検出）
    conditions = [
        func.lower(func.replace(func.replace(Patient.name, "\u3000", " "), " ", ""))
            == query_name_nospace,
    ]
    if phone_q:
        conditions.append(
            func.replace(func.replace(Patient.phone, "-", ""), "ー", "") == phone_q
        )

    result = await db.execute(
        select(Patient).where(
            and_(
                Patient.is_active == True,
                or_(*conditions),
            )
        ).limit(50)
    )
    candidates = result.scalars().all()

    responses = []
    for p in candidates:
        reasons = []
        p_full = _normalize_for_compare(p.name)
        p_full_nospace = p_full.replace(" ", "")
        p_ln = _normalize_for_compare(p.last_name)
        p_fn = _normalize_for_compare(p.first_name)

        # 氏名一致判定（同姓同名のみ）
        name_match = False
        if data.registration_mode == "split":
            ln = _normalize_for_compare(data.last_name)
            fn = _normalize_for_compare(data.first_name)
            if p_ln == ln and p_fn == fn:
                reasons.append("姓名一致")
                name_match = True
            elif p_full_nospace == query_name_nospace:
                reasons.append("氏名一致")
                name_match = True
        else:
            if p_full_nospace == query_name_nospace:
                reasons.append("氏名一致")
                name_match = True

        # 電話番号一致
        if phone_q and p.phone:
            if _normalize_phone(p.phone) == phone_q:
                reasons.append("電話番号一致")

        # 生年月日一致
        if data.birth_date and p.birth_date:
            if p.birth_date == data.birth_date:
                reasons.append("生年月日一致")

        # 同姓同名の場合のみ候補として返す（電話番号のみ一致は対象外）
        if name_match and reasons:
            responses.append(CandidateResponse(
                patient=PatientResponse.model_validate(p),
                match_reasons=reasons,
            ))

    return responses


@router.get("/", response_model=PatientPageResponse)
async def list_patients(
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
    sort_by: Literal["name", "patient_number", "created_at"] = Query("name"),
    sort_order: Literal["asc", "desc"] = Query("asc"),
    include_inactive: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    _auth: dict = Depends(require_staff),
):
    base_filter = True if include_inactive else Patient.is_active == True

    total_result = await db.execute(
        select(func.count(Patient.id)).where(base_filter)
    )
    total = total_result.scalar() or 0

    sort_col = {
        "name": Patient.reading,
        "patient_number": Patient.patient_number,
        "created_at": Patient.created_at,
    }[sort_by]
    order = sort_col.asc() if sort_order == "asc" else sort_col.desc()
    # reading が NULL の場合は末尾に回す
    nulls_order = sort_col.asc().nulls_last() if sort_order == "asc" else sort_col.desc().nulls_first()

    offset = (page - 1) * per_page
    result = await db.execute(
        select(Patient).where(base_filter).order_by(nulls_order).offset(offset).limit(per_page)
    )
    items = result.scalars().all()
    return PatientPageResponse(items=items, total=total, page=page, per_page=per_page)


@router.get("/{patient_id}", response_model=PatientResponse)
async def get_patient(patient_id: int, db: AsyncSession = Depends(get_db), _auth: dict = Depends(require_staff)):
    result = await db.execute(select(Patient).where(Patient.id == patient_id))
    patient = result.scalar_one_or_none()
    if not patient:
        raise HTTPException(status_code=404, detail="患者が見つかりません")
    return patient


@router.post("/", response_model=PatientResponse, status_code=201)
async def create_patient(data: PatientCreate, db: AsyncSession = Depends(get_db), _auth: dict = Depends(require_staff)):
    patient_number = await _generate_patient_number(db)
    name = build_name(data.registration_mode, data.last_name, data.middle_name,
                      data.first_name, data.full_name)

    # full_name モードでも互換性のため reading → kana に転記
    dump = data.model_dump(exclude={"full_name"})
    if data.registration_mode == "full_name" and data.full_name:
        dump["last_name"] = data.full_name  # 互換: last_name に格納
        dump["first_name"] = None

    patient = Patient(
        **dump,
        name=name,
        patient_number=patient_number,
    )
    db.add(patient)
    await db.commit()
    await db.refresh(patient)
    return patient


@router.put("/{patient_id}", response_model=PatientResponse)
async def update_patient(
    patient_id: int, data: PatientUpdate, db: AsyncSession = Depends(get_db), _auth: dict = Depends(require_staff)
):
    result = await db.execute(select(Patient).where(Patient.id == patient_id))
    patient = result.scalar_one_or_none()
    if not patient:
        raise HTTPException(status_code=404, detail="患者が見つかりません")

    update_data = data.model_dump(exclude_unset=True)
    full_name_val = update_data.pop("full_name", None)

    for key, value in update_data.items():
        setattr(patient, key, value)

    # name を再生成
    mode = update_data.get("registration_mode", patient.registration_mode) or "split"
    if mode == "full_name" and full_name_val:
        patient.name = full_name_val
        patient.last_name = full_name_val
        patient.first_name = None
        patient.middle_name = None
    else:
        ln = update_data.get("last_name", patient.last_name) or ""
        mn = update_data.get("middle_name", patient.middle_name) or ""
        fn = update_data.get("first_name", patient.first_name) or ""
        patient.name = " ".join(p for p in [ln, mn, fn] if p)

    await db.commit()
    await db.refresh(patient)
    return patient


@router.post("/{patient_id}/deactivate", response_model=PatientResponse)
async def deactivate_patient(patient_id: int, db: AsyncSession = Depends(get_db), _auth: dict = Depends(require_staff)):
    """患者を非表示化（論理削除）"""
    result = await db.execute(select(Patient).where(Patient.id == patient_id))
    patient = result.scalar_one_or_none()
    if not patient:
        raise HTTPException(status_code=404, detail="患者が見つかりません")
    patient.is_active = False
    await db.commit()
    await db.refresh(patient)
    return patient


@router.post("/{patient_id}/reactivate", response_model=PatientResponse)
async def reactivate_patient(patient_id: int, db: AsyncSession = Depends(get_db), _auth: dict = Depends(require_staff)):
    """患者を再有効化"""
    result = await db.execute(select(Patient).where(Patient.id == patient_id))
    patient = result.scalar_one_or_none()
    if not patient:
        raise HTTPException(status_code=404, detail="患者が見つかりません")
    patient.is_active = True
    await db.commit()
    await db.refresh(patient)
    return patient


@router.post("/{patient_id}/purge")
async def purge_patient(
    patient_id: int,
    data: PatientPurgeRequest,
    db: AsyncSession = Depends(get_db),
    _auth: dict = Depends(require_staff),
):
    """完全削除（2段階目）: 非表示化済み患者のみ削除可能。"""
    result = await db.execute(select(Patient).where(Patient.id == patient_id))
    patient = result.scalar_one_or_none()
    if not patient:
        raise HTTPException(status_code=404, detail="患者が見つかりません")
    if patient.is_active:
        raise HTTPException(status_code=400, detail="先に患者を非表示化してください")

    # 理由は必須（監査/運用ルール）
    if not data.reason.strip():
        raise HTTPException(status_code=400, detail="削除理由は必須です")

    # 過去予約は保持し、患者参照のみ切る
    await db.execute(
        update(Reservation)
        .where(Reservation.patient_id == patient_id)
        .values(patient_id=None)
    )
    await db.delete(patient)
    await db.commit()
    return {"status": "ok", "deleted_id": patient_id}
