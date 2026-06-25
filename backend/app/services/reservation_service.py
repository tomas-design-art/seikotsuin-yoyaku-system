"""予約サービス — 自動確定・競合検出・ステータス遷移"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from fastapi import HTTPException

from app.models.reservation import Reservation
from app.models.practitioner import Practitioner
from app.models.menu import Menu
from app.models.setting import Setting
from app.models.weekly_schedule import WeeklySchedule
from app.schemas.reservation import ReservationCreate, ReservationResponse, PatientBrief, MenuBrief
from app.services.conflict_detector import check_conflict, check_patient_conflict, ACTIVE_STATUSES
from app.services.notification_service import create_notification
from app.services.schedule_service import is_practitioner_working, get_practitioner_working_hours
from app.services.business_hours import get_business_hours_for_date
from app.models.practitioner_unavailable_time import PractitionerUnavailableTime
from app.utils.datetime_jst import now_jst, JST

logger = logging.getLogger(__name__)

VALID_TRANSITIONS = {
    "PENDING": {"CONFIRMED", "REJECTED", "EXPIRED", "CANCEL_REQUESTED", "CANCELLED"},
    "HOLD": {"CONFIRMED", "EXPIRED", "CANCELLED"},
    "CONFIRMED": {"CHANGE_REQUESTED", "CANCEL_REQUESTED"},
    "CHANGE_REQUESTED": {"CANCELLED"},
    "CANCEL_REQUESTED": {"CANCELLED"},
}

TERMINAL_STATUSES = {"CANCELLED", "REJECTED", "EXPIRED"}


async def get_setting_value(db: AsyncSession, key: str, default: str = "") -> str:
    result = await db.execute(select(Setting).where(Setting.key == key))
    setting = result.scalar_one_or_none()
    return setting.value if setting else default


async def validate_business_hours(db: AsyncSession, start_time: datetime, end_time: datetime):
    """営業時間チェック: date_override → 祝日 → 曜日 の3段階判定"""
    bh = await get_business_hours_for_date(db, start_time.date())

    if not bh.is_open:
        detail = "休診日のため予約できません"
        if bh.label:
            detail = f"{bh.label}のため予約できません"
        raise HTTPException(status_code=400, detail=detail)

    bh_start, bh_end = bh.to_minutes()
    start_minutes = start_time.hour * 60 + start_time.minute
    end_minutes = end_time.hour * 60 + end_time.minute

    if start_minutes < bh_start or end_minutes > bh_end:
        raise HTTPException(status_code=400, detail="営業時間外の予約です")


async def validate_practitioner(db: AsyncSession, practitioner_id: int):
    result = await db.execute(
        select(Practitioner).where(
            Practitioner.id == practitioner_id, Practitioner.is_active == True
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="無効な施術者です")


async def determine_status(
    db: AsyncSession, data: ReservationCreate
) -> str:
    """自動確定判定"""
    # メニュー未選択は仮予約条件にしない。
    # 手入力運用ではメニュー後追い編集を許容しつつ、競合がなければ確定扱いにする。
    if not data.practitioner_id:
        return "PENDING"

    # 施術者競合チェック
    conflicts = await check_conflict(
        db, data.practitioner_id, data.start_time, data.end_time
    )
    if conflicts:
        return "PENDING"

    # 同一患者ダブルブッキングチェック
    patient_conflicts = await check_patient_conflict(
        db, data.patient_id, data.start_time, data.end_time
    )
    if patient_conflicts:
        return "PENDING"

    return "CONFIRMED"


def build_reservation_response(reservation: Reservation) -> dict:
    """ReservationレスポンスDictを構築"""
    resp = {
        "id": reservation.id,
        "patient": None,
        "practitioner_id": reservation.practitioner_id,
        "practitioner_name": None,
        "menu": None,
        "color": None,
        "color_id": reservation.color_id,
        "start_time": reservation.start_time,
        "end_time": reservation.end_time,
        "status": reservation.status,
        "channel": reservation.channel,
        "source_ref": reservation.source_ref,
        "notes": reservation.notes,
        "conflict_note": reservation.conflict_note,
        "hotpepper_synced": reservation.hotpepper_synced,
        "synced_by": reservation.synced_by,
        "hold_expires_at": reservation.hold_expires_at,
        "series_id": reservation.series_id,
        "series_info": None,
        "created_at": reservation.created_at,
        "updated_at": reservation.updated_at,
    }
    if reservation.series_id and reservation.series:
        s = reservation.series
        resp["series_info"] = {
            "id": s.id,
            "frequency": s.frequency,
            "total_created": s.total_created,
            "remaining_count": s.remaining_count,
            "is_active": s.is_active,
        }
    if reservation.patient:
        resp["patient"] = {
            "id": reservation.patient.id,
            "name": reservation.patient.name,
            "last_name": reservation.patient.last_name,
            "first_name": reservation.patient.first_name,
            "last_name_kana": reservation.patient.last_name_kana,
            "patient_number": reservation.patient.patient_number,
        }
    if reservation.menu:
        resp["menu"] = {
            "id": reservation.menu.id,
            "name": reservation.menu.name,
            "duration_minutes": reservation.menu.duration_minutes,
        }
    if reservation.practitioner:
        resp["practitioner_name"] = reservation.practitioner.name
    if reservation.color:
        resp["color"] = {
            "id": reservation.color.id,
            "name": reservation.color.name,
            "color_code": reservation.color.color_code,
        }
    return resp


async def create_reservation(
    db: AsyncSession, data: ReservationCreate
) -> dict:
    """予約登録"""
    # patient_id=0 は無効 → Noneに正規化
    if data.patient_id is not None and data.patient_id <= 0:
        data.patient_id = None

    # color_id が未指定ならメニューの色を引き継ぐ（LINE/Web等の自動登録向け）
    resolved_color_id = data.color_id
    if resolved_color_id is None and data.menu_id is not None:
        menu_row = await db.execute(select(Menu).where(Menu.id == data.menu_id))
        menu = menu_row.scalar_one_or_none()
        if menu and menu.color_id is not None:
            resolved_color_id = menu.color_id

    # バリデーション
    await validate_business_hours(db, data.start_time, data.end_time)
    await validate_practitioner(db, data.practitioner_id)

    # 職員勤務スケジュールチェック
    target_date = data.start_time.date()
    working, reason, _ = await is_practitioner_working(db, data.practitioner_id, target_date)
    if not working:
        detail = f"この施術者は{target_date}は休みです"
        if reason:
            detail += f"（理由: {reason}）"
        raise HTTPException(status_code=400, detail=detail)

    # 施術者の勤務時間チェック（時短勤務対応）
    p_start, p_end = await get_practitioner_working_hours(db, data.practitioner_id, target_date)
    if p_start and p_end:
        p_sh, p_sm = map(int, p_start.split(":"))
        p_eh, p_em = map(int, p_end.split(":"))
        p_start_min = p_sh * 60 + p_sm
        p_end_min = p_eh * 60 + p_em
        start_minutes = data.start_time.hour * 60 + data.start_time.minute
        end_minutes = data.end_time.hour * 60 + data.end_time.minute
        if start_minutes < p_start_min or end_minutes > p_end_min:
            raise HTTPException(
                status_code=400,
                detail=f"この施術者の勤務時間は{p_start}〜{p_end}です",
            )

    # 施術者の時間帯休みチェック
    from sqlalchemy import and_
    start_minutes = data.start_time.hour * 60 + data.start_time.minute
    end_minutes = data.end_time.hour * 60 + data.end_time.minute
    ut_result = await db.execute(
        select(PractitionerUnavailableTime).where(
            and_(
                PractitionerUnavailableTime.practitioner_id == data.practitioner_id,
                PractitionerUnavailableTime.date == target_date,
            )
        )
    )
    for ut in ut_result.scalars().all():
        ut_sh, ut_sm = map(int, ut.start_time.split(":"))
        ut_eh, ut_em = map(int, ut.end_time.split(":"))
        ut_start = ut_sh * 60 + ut_sm
        ut_end = ut_eh * 60 + ut_em
        if start_minutes < ut_end and end_minutes > ut_start:
            detail = f"この施術者は{ut.start_time}〜{ut.end_time}は不在です"
            if ut.reason:
                detail += f"（{ut.reason}）"
            raise HTTPException(status_code=400, detail=detail)

    # チャネル分類
    # - オンライン系（HOTPEPPER/LINE/CHATBOT）: タイムラグで競合しうるため登録を許可し、赤帯で警告表示
    # - 手入力系（PHONE/WALK_IN/その他）: 同一枠の重複予約は絶対に作成させない（409拒否）
    ONLINE_CHANNELS = ("HOTPEPPER", "LINE", "CHATBOT", "WEB")
    is_online = (data.channel or "") in ONLINE_CHANNELS

    # 施術者競合の事前チェック
    practitioner_conflicts = await check_conflict(
        db, data.practitioner_id, data.start_time, data.end_time
    )
    # 同一患者ダブルブッキングチェック
    patient_conflicts = await check_patient_conflict(
        db, data.patient_id, data.start_time, data.end_time
    )

    if not is_online and (practitioner_conflicts or patient_conflicts):
        # 手入力予約では競合を決して許さない → 409で拒否
        from app.services.schedule_service import find_transfer_candidates
        conflict_list = [
            {
                "id": c.id,
                "patient_name": c.patient.name if c.patient else None,
                "practitioner_name": c.practitioner.name if getattr(c, "practitioner", None) else None,
                "start_time": c.start_time.astimezone(JST).isoformat(),
                "end_time": c.end_time.astimezone(JST).isoformat(),
                "status": c.status,
            }
            for c in practitioner_conflicts
        ]
        patient_conflict_list = [
            {
                "id": c.id,
                "patient_name": c.patient.name if c.patient else None,
                "practitioner_name": c.practitioner.name if c.practitioner else None,
                "start_time": c.start_time.astimezone(JST).isoformat(),
                "end_time": c.end_time.astimezone(JST).isoformat(),
                "status": c.status,
            }
            for c in patient_conflicts
        ]
        # 別施術者候補（同時間帯で空いている施術者）を検索してスライド提案
        alternative_practitioners: list[dict] = []
        try:
            candidates = await find_transfer_candidates(
                db,
                data.practitioner_id,
                data.start_time.astimezone(JST).date(),
                data.start_time,
                data.end_time,
            )
            alternative_practitioners = [c for c in candidates if c.get("is_available")]
        except Exception:
            alternative_practitioners = []

        if practitioner_conflicts and patient_conflicts:
            msg = "同じ枠にすでに予約があり、かつ同一患者が同時間帯に別予約を持っています"
        elif practitioner_conflicts:
            msg = "この施術者の同じ時間帯にはすでに予約があります"
        else:
            msg = "この患者は同時間帯にすでに別の予約があります"

        raise HTTPException(
            status_code=409,
            detail={
                "detail": msg,
                "conflicting_reservations": conflict_list,
                "patient_conflicts": patient_conflict_list,
                "alternative_practitioners": alternative_practitioners,
            },
        )

    # ここから下は is_online、または手入力で競合なしのケース
    conflict_note: str | None = None
    if is_online:
        status = "CONFIRMED"
        if practitioner_conflicts:
            conflict_names = [
                f"{(c.patient.name if c.patient else '不明')}"
                f"({c.start_time.astimezone(JST).strftime('%H:%M')}-{c.end_time.astimezone(JST).strftime('%H:%M')})"
                for c in practitioner_conflicts
            ]
            conflict_note = "競合: " + ", ".join(conflict_names)
        if patient_conflicts:
            pc_names = [
                f"{(c.practitioner.name if c.practitioner else '不明')}"
                f"({c.start_time.astimezone(JST).strftime('%H:%M')}-{c.end_time.astimezone(JST).strftime('%H:%M')})"
                for c in patient_conflicts
            ]
            pc_msg = "患者ダブルブッキング: " + ", ".join(pc_names)
            conflict_note = f"{conflict_note} / {pc_msg}" if conflict_note else pc_msg
    else:
        # 手入力で競合なし → 通常フロー
        status = await determine_status(db, data)

    reservation = Reservation(
        patient_id=data.patient_id,
        practitioner_id=data.practitioner_id,
        menu_id=data.menu_id,
        color_id=resolved_color_id,
        start_time=data.start_time,
        end_time=data.end_time,
        status=status,
        channel=data.channel,
        source_ref=data.source_ref,
        notes=data.notes,
        conflict_note=conflict_note,
        hotpepper_synced=(data.channel == "HOTPEPPER"),
    )

    try:
        db.add(reservation)
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        if "no_overlap" in str(e.orig):
            conflicts = await check_conflict(
                db, data.practitioner_id, data.start_time, data.end_time
            )
            conflict_list = []
            for c in conflicts:
                conflict_list.append({
                    "id": c.id,
                    "patient_name": c.patient.name if c.patient else None,
                    "start_time": c.start_time.astimezone(JST).isoformat(),
                    "end_time": c.end_time.astimezone(JST).isoformat(),
                    "status": c.status,
                })
            raise HTTPException(
                status_code=409,
                detail={
                    "detail": "予約が競合しています",
                    "conflicting_reservations": conflict_list,
                },
            )
        raise

    # 通知
    patient_name = ""
    if data.patient_id:
        from app.models.patient import Patient
        pt_result = await db.execute(
            select(Patient).where(Patient.id == data.patient_id)
        )
        pt = pt_result.scalar_one_or_none()
        if pt:
            patient_name = pt.name
    await create_notification(
        db, "new_reservation",
        f"新規予約: {patient_name} {data.start_time.strftime('%H:%M')}-{data.end_time.strftime('%H:%M')}",
        reservation.id,
        extra_data={"channel": data.channel},
    )
    # HP枠押さえリマインド（HOTPEPPER以外のチャネルで登録時）
    if data.channel != "HOTPEPPER":
        date_str = data.start_time.strftime('%m/%d')
        time_str = f"{data.start_time.strftime('%H:%M')}-{data.end_time.strftime('%H:%M')}"
        await create_notification(
            db, "hotpepper_sync_reminder",
            f"HotPepper側の {date_str} {time_str} を押さえてください",
            reservation.id,
        )
    if conflict_note and is_online:
        channel_label = {"HOTPEPPER": "HotPepper", "LINE": "LINE", "CHATBOT": "Web"}.get(data.channel or "", "オンライン")
        await create_notification(
            db, "conflict_detected",
            f"{channel_label}予約が競合しています: {conflict_note}",
            reservation.id,
        )
    if patient_conflicts:
        # 既存の競合予約にもconflict_noteを付与
        for c in patient_conflicts:
            if not c.conflict_note:
                c.conflict_note = f"患者ダブルブッキング: {patient_name}が同時間帯に別予約あり"
            elif "患者ダブルブッキング" not in c.conflict_note:
                c.conflict_note += f" / 患者ダブルブッキング: {patient_name}が同時間帯に別予約あり"
        await create_notification(
            db, "conflict_detected",
            f"患者ダブルブッキング: {patient_name} が同時間帯に複数予約されています",
            reservation.id,
        )

    await db.commit()
    result = await db.execute(
        select(Reservation)
        .where(Reservation.id == reservation.id)
        .options(
            selectinload(Reservation.patient),
            selectinload(Reservation.practitioner),
            selectinload(Reservation.menu),
            selectinload(Reservation.color),
        )
    )
    reservation = result.scalar_one()
    return build_reservation_response(reservation)


async def refresh_conflict_notes_for_overlapping(
    db: AsyncSession, reservation: Reservation
) -> None:
    """予約がキャンセル/却下等で非アクティブになった際、
    その予約と時間帯が重なっていた他の予約の conflict_note を再評価してクリアする。"""
    # この予約と時間が重なり、conflict_noteがある予約を取得
    result = await db.execute(
        select(Reservation)
        .where(
            Reservation.conflict_note.isnot(None),
            Reservation.status.in_(ACTIVE_STATUSES),
            Reservation.id != reservation.id,
            # 施術者競合 OR 患者競合の可能性がある予約
            Reservation.start_time < reservation.end_time,
            Reservation.end_time > reservation.start_time,
        )
        .options(selectinload(Reservation.patient), selectinload(Reservation.practitioner))
    )
    candidates = result.scalars().all()

    for r in candidates:
        # 施術者競合を再チェック
        prac_conflicts = await check_conflict(
            db, r.practitioner_id, r.start_time, r.end_time,
            exclude_reservation_id=r.id,
        )
        # 患者競合を再チェック
        pat_conflicts = []
        if r.patient_id:
            pat_conflicts = await check_patient_conflict(
                db, r.patient_id, r.start_time, r.end_time,
                exclude_reservation_id=r.id,
            )

        # 新しいconflict_noteを構築
        parts = []
        if prac_conflicts:
            names = [
                f"{(c.patient.name if c.patient else '不明')}"
                f"({c.start_time.astimezone(JST).strftime('%H:%M')}-{c.end_time.astimezone(JST).strftime('%H:%M')})"
                for c in prac_conflicts
            ]
            parts.append("競合: " + ", ".join(names))
        if pat_conflicts:
            names = [
                f"{(c.practitioner.name if c.practitioner else '不明')}"
                f"({c.start_time.astimezone(JST).strftime('%H:%M')}-{c.end_time.astimezone(JST).strftime('%H:%M')})"
                for c in pat_conflicts
            ]
            parts.append("患者ダブルブッキング: " + ", ".join(names))

        r.conflict_note = " / ".join(parts) if parts else None


_STATUS_LABELS = {
    "PENDING": "仮予約",
    "HOLD": "一時確保",
    "CONFIRMED": "確定",
    "CHANGE_REQUESTED": "変更申請中",
    "CANCEL_REQUESTED": "キャンセル申請中",
    "CANCELLED": "キャンセル済",
    "REJECTED": "却下",
    "EXPIRED": "期限切れ",
}


async def transition_status(
    db: AsyncSession, reservation_id: int, new_status: str
) -> Reservation:
    """ステータス遷移（バリデーション付き）

    冪等性: 同一ステータスへの遷移はno-op（エラーにしない）。
    二重クリックやUIの状態ズレによる不要な失敗を防ぐ。
    """
    result = await db.execute(
        select(Reservation).where(Reservation.id == reservation_id)
    )
    reservation = result.scalar_one_or_none()
    if not reservation:
        raise HTTPException(status_code=404, detail="予約が見つかりません")

    current = reservation.status

    # 冪等: 既に目的のステータスなら何もしない
    if current == new_status:
        return reservation

    if current in TERMINAL_STATUSES:
        cur_label = _STATUS_LABELS.get(current, current)
        raise HTTPException(
            status_code=400,
            detail=f"この予約は既に「{cur_label}」のため操作できません",
        )

    allowed = VALID_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        cur_label = _STATUS_LABELS.get(current, current)
        new_label = _STATUS_LABELS.get(new_status, new_status)
        raise HTTPException(
            status_code=400,
            detail=f"「{cur_label}」状態の予約は「{new_label}」に変更できません",
        )

    reservation.status = new_status

    # 非アクティブ化した場合、関連予約のconflict_noteを再評価
    if new_status in TERMINAL_STATUSES:
        reservation.conflict_note = None  # 自身のconflict_noteもクリア
        await refresh_conflict_notes_for_overlapping(db, reservation)

    await db.flush()
    return reservation


async def handle_change_request(
    db: AsyncSession, reservation_id: int,
    new_start_time: datetime, new_end_time: datetime,
    new_practitioner_id: int | None = None,
) -> dict:
    """変更申請: 旧予約→CHANGE_REQUESTED、新時間帯→HOLD"""
    result = await db.execute(
        select(Reservation).where(Reservation.id == reservation_id)
    )
    old_reservation = result.scalar_one_or_none()
    if not old_reservation:
        raise HTTPException(status_code=404, detail="予約が見つかりません")
    if old_reservation.status != "CONFIRMED":
        raise HTTPException(status_code=400, detail="CONFIRMED状態の予約のみ変更申請できます")

    practitioner_id = new_practitioner_id or old_reservation.practitioner_id
    hold_duration = int(await get_setting_value(db, "hold_duration_minutes", "10"))

    # 新時間帯をHOLDとして確保
    hold_reservation = Reservation(
        patient_id=old_reservation.patient_id,
        practitioner_id=practitioner_id,
        menu_id=old_reservation.menu_id,
        start_time=new_start_time,
        end_time=new_end_time,
        status="HOLD",
        channel=old_reservation.channel,
        notes=f"変更元: 予約#{reservation_id}",
        hold_expires_at=now_jst() + timedelta(minutes=hold_duration),
    )

    try:
        db.add(hold_reservation)
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        if "no_overlap" in str(e.orig):
            raise HTTPException(
                status_code=409, detail="変更先の時間帯が競合しています"
            )
        raise

    old_reservation.status = "CHANGE_REQUESTED"

    await create_notification(
        db, "change_requested",
        f"変更申請: 予約#{reservation_id} → {new_start_time.strftime('%m/%d %H:%M')}-{new_end_time.strftime('%H:%M')}",
        reservation_id,
    )

    await db.commit()
    result = await db.execute(
        select(Reservation)
        .where(Reservation.id == hold_reservation.id)
        .options(
            selectinload(Reservation.patient),
            selectinload(Reservation.practitioner),
            selectinload(Reservation.menu),
            selectinload(Reservation.color),
        )
    )
    hold_reservation = result.scalar_one()
    return {
        "old_reservation_id": reservation_id,
        "old_status": "CHANGE_REQUESTED",
        "hold_reservation": build_reservation_response(hold_reservation),
    }


async def handle_change_approve(db: AsyncSession, reservation_id: int) -> dict:
    """変更承認: 旧CANCELLED、新CONFIRMED"""
    result = await db.execute(
        select(Reservation).where(Reservation.id == reservation_id)
    )
    old_reservation = result.scalar_one_or_none()
    if not old_reservation:
        raise HTTPException(status_code=404, detail="予約が見つかりません")
    if old_reservation.status != "CHANGE_REQUESTED":
        raise HTTPException(status_code=400, detail="CHANGE_REQUESTED状態の予約のみ承認できます")

    # HOLD予約を探す（notesに変更元IDが含まれる）
    hold_result = await db.execute(
        select(Reservation).where(
            Reservation.status == "HOLD",
            Reservation.notes.like(f"%変更元: 予約#{reservation_id}%"),
        )
    )
    hold_reservation = hold_result.scalar_one_or_none()

    old_reservation.status = "CANCELLED"
    old_reservation.conflict_note = None
    await refresh_conflict_notes_for_overlapping(db, old_reservation)

    if hold_reservation:
        hold_reservation.status = "CONFIRMED"
        hold_reservation.hold_expires_at = None

    await create_notification(
        db, "change_approved",
        f"変更承認: 予約#{reservation_id}",
        reservation_id,
    )

    await db.commit()
    return {
        "old_reservation_id": reservation_id,
        "old_status": "CANCELLED",
        "new_reservation_id": hold_reservation.id if hold_reservation else None,
        "new_status": "CONFIRMED" if hold_reservation else None,
    }


async def reschedule_reservation(
    db: AsyncSession, reservation_id: int,
    new_start_time: datetime, new_end_time: datetime,
    new_practitioner_id: int | None = None,
) -> dict:
    """予約変更（直接）: 日時・施術者を変更し、変更ログを備考に追記"""
    result = await db.execute(
        select(Reservation).where(Reservation.id == reservation_id)
    )
    reservation = result.scalar_one_or_none()
    if not reservation:
        raise HTTPException(status_code=404, detail="予約が見つかりません")
    if reservation.status in TERMINAL_STATUSES:
        raise HTTPException(status_code=400, detail="終了済みの予約は変更できません")

    # バリデーション
    await validate_business_hours(db, new_start_time, new_end_time)
    practitioner_id = new_practitioner_id or reservation.practitioner_id
    await validate_practitioner(db, practitioner_id)

    # 施術者競合チェック（自分自身を除外）
    conflicts = await check_conflict(
        db, practitioner_id, new_start_time, new_end_time,
        exclude_reservation_id=reservation_id,
    )
    if conflicts:
        conflict_names = []
        for c in conflicts:
            name = c.patient.name if c.patient else "不明"
            conflict_names.append(
                f"{name}({c.start_time.astimezone(JST).strftime('%H:%M')}-{c.end_time.astimezone(JST).strftime('%H:%M')})"
            )
        raise HTTPException(
            status_code=409,
            detail=f"予約が競合しています: {', '.join(conflict_names)}",
        )

    # 変更ログ作成
    old_date_str = reservation.start_time.astimezone(JST).strftime("%Y/%m/%d %H:%M")
    change_time_str = now_jst().strftime("%Y/%m/%d %H:%M")
    change_log = f"{old_date_str}から予約変更（{change_time_str}）"

    # 備考に追記
    if reservation.notes:
        reservation.notes = reservation.notes + "\n" + change_log
    else:
        reservation.notes = change_log

    # 更新
    reservation.start_time = new_start_time
    reservation.end_time = new_end_time
    reservation.practitioner_id = practitioner_id

    reservation.conflict_note = None

    await create_notification(
        db, "reservation_changed",
        f"予約変更: 予約#{reservation_id} → {new_start_time.strftime('%m/%d %H:%M')}-{new_end_time.strftime('%H:%M')}",
        reservation_id,
    )

    await db.commit()
    result = await db.execute(
        select(Reservation)
        .where(Reservation.id == reservation_id)
        .options(
            selectinload(Reservation.patient),
            selectinload(Reservation.practitioner),
            selectinload(Reservation.menu),
            selectinload(Reservation.color),
        )
    )
    reservation = result.scalar_one()
    return build_reservation_response(reservation)
