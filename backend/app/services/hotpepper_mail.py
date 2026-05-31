"""HotPepperメール取得アダプター + 予約登録サービス"""
import asyncio
import hashlib
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.menu import Menu
from app.models.patient import Patient
from app.models.practitioner import Practitioner
from app.models.practitioner_unavailable_time import PractitionerUnavailableTime
from app.models.reservation_color import ReservationColor
from app.models.reservation import Reservation
from app.models.setting import Setting
from app.services.notification_service import create_notification
from app.agents.mail_parser import ai_review_hotpepper_required, parse_hotpepper_mail
from app.services.conflict_detector import check_conflict
from app.services.imap_adapter import IMAPAdapter, IMAPFetchedMail
from app.services.schedule_service import is_practitioner_working
from app.database import async_session
from app.utils.datetime_jst import JST

logger = logging.getLogger(__name__)

PROCESSED_MID_HASHES_KEY = "hotpepper_processed_mid_hashes"
FAILED_MID_COUNTS_KEY = "hotpepper_failed_mid_counts"
MAX_PROCESSED_HASHES = 1000
MAX_FAILED_TRACKED = 2000
DEAD_LETTER_RETRY_LIMIT = 3
HOTPEPPER_FIXED_COLOR_CODE = "#f2740d"
HOTPEPPER_MENU_NAME = "ホットペッパー"


@dataclass
class Email:
    subject: str
    body: str
    sender: str
    received_at: datetime
    message_id: str


class MailFetcher(ABC):
    """メール取得の抽象クラス"""

    @abstractmethod
    async def fetch_new_emails(self, since: datetime) -> list[Email]:
        ...


class GmailFetcher(MailFetcher):
    """Gmail API (OAuth2) でメールを取得"""

    def __init__(self, credentials_path: Optional[str] = None):
        self.credentials_path = credentials_path

    async def fetch_new_emails(self, since: datetime) -> list[Email]:
        # Gmail API実装（OAuth2設定後に有効化）
        logger.info("Gmail fetcher not yet configured")
        return []


class IMAPFetcher(MailFetcher):
    """汎用IMAP でメールを取得"""

    def __init__(self, host: str, port: int, username: str, password: str, use_ssl: bool = True):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_ssl = use_ssl

    async def fetch_new_emails(self, since: datetime) -> list[Email]:
        # IMAP実装
        logger.info("IMAP fetcher not yet configured")
        return []


def get_mail_fetcher(provider: str = "gmail") -> MailFetcher:
    """環境変数に基づいてメールフェッチャーを返す"""
    if provider == "gmail":
        return GmailFetcher()
    elif provider == "imap":
        return IMAPFetcher(
            host="imap.example.com",
            port=993,
            username="",
            password="",
        )
    else:
        raise ValueError(f"Unknown mail provider: {provider}")


def _message_id_hash(message_id: str) -> str:
    return hashlib.sha1(message_id.encode("utf-8", errors="ignore")).hexdigest()[:12]


async def _get_setting(db: AsyncSession, key: str, default: str = "") -> str:
    result = await db.execute(select(Setting).where(Setting.key == key))
    row = result.scalar_one_or_none()
    return row.value if row else default


async def _set_setting(db: AsyncSession, key: str, value: str):
    result = await db.execute(select(Setting).where(Setting.key == key))
    row = result.scalar_one_or_none()
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))
    await db.flush()


async def _load_processed_mid_hashes(db: AsyncSession) -> list[str]:
    raw = await _get_setting(db, PROCESSED_MID_HASHES_KEY, "")
    if not raw:
        return []
    return [x for x in raw.split(",") if x]


async def _save_processed_mid_hashes(db: AsyncSession, hashes: list[str]):
    compact = ",".join(hashes[-MAX_PROCESSED_HASHES:])
    await _set_setting(db, PROCESSED_MID_HASHES_KEY, compact)


async def _load_failed_mid_counts(db: AsyncSession) -> dict[str, int]:
    raw = await _get_setting(db, FAILED_MID_COUNTS_KEY, "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        out: dict[str, int] = {}
        for k, v in data.items():
            try:
                out[str(k)] = int(v)
            except Exception:
                continue
        return out
    except Exception:
        logger.warning("failed to parse %s; reset", FAILED_MID_COUNTS_KEY)
        return {}


async def _save_failed_mid_counts(db: AsyncSession, counts: dict[str, int]):
    items = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:MAX_FAILED_TRACKED]
    compact = json.dumps({k: v for k, v in items}, ensure_ascii=False)
    await _set_setting(db, FAILED_MID_COUNTS_KEY, compact)


def _sender_filters_from_settings() -> list[str]:
    if settings.hotpepper_sender_filters:
        return [x.strip() for x in settings.hotpepper_sender_filters.split(",") if x.strip()]
    return ["hotpepper.jp", "beauty.hotpepper.jp", "salonboard"]


async def poll_hotpepper_mail_once() -> dict:
    """iCloud/IMAP からHotPepperメールを取得して処理する。"""
    if settings.mail_provider.lower() not in {"imap", "icloud", "icloud_imap", "icloud-imap"}:
        return {"status": "skipped", "reason": f"mail_provider={settings.mail_provider}"}

    if not settings.icloud_email or not settings.icloud_app_password:
        logger.warning("ICLOUD_EMAIL/ICLOUD_APP_PASSWORD が未設定のためポーリングをスキップ")
        return {"status": "skipped", "reason": "icloud_credentials_missing"}

    adapter = IMAPAdapter(
        host=settings.imap_host,
        port=settings.imap_port,
        username=settings.icloud_email,
        password=settings.icloud_app_password,
        mailbox=settings.imap_mailbox,
    )

    max_connect_retries = max(1, settings.hotpepper_poll_max_retries)
    base_delay = max(1, settings.hotpepper_poll_retry_base_seconds)
    emails: list[IMAPFetchedMail] = []
    sender_filters = _sender_filters_from_settings()

    for attempt in range(1, max_connect_retries + 1):
        try:
            await asyncio.to_thread(adapter.connect)
            emails = await asyncio.to_thread(
                adapter.fetch_hotpepper_mails,
                sender_filters,
                limit=settings.hotpepper_poll_fetch_limit,
                search_days=settings.hotpepper_poll_search_days,
            )
            break
        except Exception as e:
            logger.exception("HotPepper IMAP poll failed (attempt=%s/%s): %s", attempt, max_connect_retries, e)
            if attempt >= max_connect_retries:
                await asyncio.to_thread(adapter.close)
                return {"status": "error", "reason": str(e), "attempts": attempt}
            await asyncio.sleep(base_delay * (2 ** (attempt - 1)))

    processed = 0
    skipped = 0
    failed = 0
    dead_lettered = 0

    try:
        async with async_session() as db:
            processed_hashes = await _load_processed_mid_hashes(db)
            failed_counts = await _load_failed_mid_counts(db)
            seen_set = set(processed_hashes)

            for mail in emails:
                mid = mail.message_id or f"uid:{mail.uid}"
                mh = _message_id_hash(mid)

                if mh in seen_set:
                    skipped += 1
                    await asyncio.to_thread(adapter.mark_seen, mail.uid)
                    continue

                result = await process_hotpepper_email(db, mail.body)
                status = result.get("status")
                if status in {"created", "changed", "cancelled", "skipped"}:
                    processed += 1
                    seen_set.add(mh)
                    failed_counts.pop(mh, None)
                    await asyncio.to_thread(adapter.mark_seen, mail.uid)
                else:
                    failed += 1
                    mail_fail_count = failed_counts.get(mh, 0) + 1
                    failed_counts[mh] = mail_fail_count
                    if mail_fail_count >= DEAD_LETTER_RETRY_LIMIT:
                        dead_lettered += 1
                        seen_set.add(mh)
                        logger.error(
                            "HotPepper mail dead-lettered: uid=%s message_id=%s hash=%s retries=%s",
                            mail.uid,
                            mail.message_id,
                            mh,
                            mail_fail_count,
                        )
                        await asyncio.to_thread(adapter.mark_seen, mail.uid)
                        failed_counts.pop(mh, None)

            await _save_processed_mid_hashes(db, list(seen_set))
            await _save_failed_mid_counts(db, failed_counts)
            await db.commit()
    finally:
        await asyncio.to_thread(adapter.close)

    return {
        "status": "ok",
        "fetched": len(emails),
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
        "dead_lettered": dead_lettered,
    }


# ---------------------------------------------------------------------------
# HotPepper メール → 予約登録
# ---------------------------------------------------------------------------


async def process_hotpepper_email(db: AsyncSession, email_body: str) -> dict:
    """HotPepper メール本文を解析し、予約を登録/更新/キャンセルする。

    Returns:
        dict: {"status": "created"|"cancelled"|"changed"|"skipped"|"error", ...}
    """
    logger.info("HotPepper メール受信 — パース開始")

    # ── 1. パース ──
    try:
        parsed = parse_hotpepper_mail(email_body)
        logger.info(
            f"パース成功: event={parsed['event_type']}, "
            f"予約番号={parsed['reservation_number']}, 患者名={parsed['patient_name']}"
        )

        # 必須項目監査（ルールベース）
        missing = _validate_required_for_reflection(parsed)

        # AI監査 + 必要時補完
        if missing:
            try:
                ai_result = await ai_review_hotpepper_required(email_body, parsed)
                parsed = _apply_ai_patch(parsed, ai_result)
                missing = _validate_required_for_reflection(parsed)
            except Exception as ai_err:
                logger.warning("HotPepper AI監査はスキップ/失敗: %s", ai_err)

        if missing:
            msg = "ホットペッパー予約者のシステム反映がされていません。予約情報が取得できませんでした"
            jp_missing = _missing_fields_to_japanese(missing)
            await create_notification(db, "hotpepper_parse_failed", f"{msg}（不足: {', '.join(jp_missing)}）")
            try:
                from app.services.line_alerts import push_admin_hotpepper_failure

                await push_admin_hotpepper_failure(f"{msg}（不足: {', '.join(jp_missing)}）", email_body)
            except Exception as notify_err:
                logger.error("HotPepper parse failure LINE通知に失敗: %s", notify_err)
            await db.commit()
            return {"status": "error", "reason": "required_fields_missing", "missing": missing}
    except ValueError as e:
        logger.error(f"パース失敗: {e}")
        try:
            from app.services.line_alerts import push_admin_hotpepper_failure

            await push_admin_hotpepper_failure(str(e), email_body)
        except Exception as notify_err:
            logger.error("HotPepper parse failure LINE通知に失敗: %s", notify_err)
        return {"status": "error", "reason": str(e)}

    event_type = parsed["event_type"]

    # ── イベント種別ルーティング ──
    if event_type == "cancelled":
        return await _handle_cancelled(db, parsed)
    elif event_type == "changed":
        return await _handle_changed(db, parsed)
    else:
        return await _handle_created(db, parsed)


def _validate_required_for_reflection(parsed: dict) -> list[str]:
    missing: list[str] = []
    if not parsed.get("patient_name"):
        missing.append("name")
    if not parsed.get("start_time"):
        missing.append("reservation_datetime")
    duration = parsed.get("duration_minutes")
    if not isinstance(duration, int) or duration <= 0:
        missing.append("duration_minutes")
    if parsed.get("duration_extracted") is False:
        missing.append("duration_minutes")
    if parsed.get("practitioner_preference_known") is not True:
        missing.append("practitioner_preference")
    return sorted(set(missing))


def _missing_fields_to_japanese(missing: list[str]) -> list[str]:
    labels = {
        "name": "名前",
        "reservation_datetime": "予約日時",
        "duration_minutes": "施術時間",
        "practitioner_preference": "担当者希望",
    }
    return [labels.get(k, k) for k in missing]


def _apply_ai_patch(parsed: dict, ai_result: dict) -> dict:
    fields = ai_result.get("fields") if isinstance(ai_result, dict) else None
    if not isinstance(fields, dict):
        return parsed

    out = dict(parsed)
    if not out.get("patient_name") and fields.get("patient_name"):
        out["patient_name"] = str(fields.get("patient_name")).strip()

    if (not out.get("start_time")) and fields.get("reservation_date") and fields.get("reservation_time"):
        try:
            out["start_time"] = datetime.strptime(
                f"{fields['reservation_date']} {fields['reservation_time']}",
                "%Y-%m-%d %H:%M",
            ).replace(tzinfo=JST)
        except Exception:
            pass

    duration = fields.get("duration_minutes")
    if isinstance(duration, (int, float)) and int(duration) > 0:
        out["duration_minutes"] = int(duration)
        out["duration_extracted"] = True

    if "practitioner_preference_known" in fields and fields.get("practitioner_preference_known") is not None:
        out["practitioner_preference_known"] = bool(fields.get("practitioner_preference_known"))

    if not out.get("practitioner_name") and fields.get("practitioner_name"):
        out["practitioner_name"] = str(fields.get("practitioner_name")).strip()

    # start_time が補完された場合のみ end_time を再計算
    if out.get("start_time") and isinstance(out.get("duration_minutes"), int):
        try:
            out["end_time"] = out["start_time"] + timedelta(minutes=int(out["duration_minutes"]))
        except Exception:
            pass

    return out


async def _handle_created(db: AsyncSession, parsed: dict) -> dict:
    """新規予約の登録"""
    # ── 重複チェック ──
    existing = await db.execute(
        select(Reservation).where(Reservation.source_ref == parsed["reservation_number"])
    )
    if existing.scalar_one_or_none():
        logger.info(f"重複スキップ: source_ref={parsed['reservation_number']} は登録済み")
        return {"status": "skipped", "reason": "duplicate", "reservation_number": parsed["reservation_number"]}

    # ── シャドーモード: ダミー患者で登録（本番 shadow_mode=False では通らない） ──
    if settings.shadow_mode:
        patient = await _get_or_create_hotpepper_dummy_patient(db)
    else:
        patient = await _find_or_create_patient(db, parsed["patient_name"], reading=parsed.get("patient_reading"))
    hotpepper_color_id = await _resolve_hotpepper_color_id(db)

    # 手動登録済みの同一患者・同一時間枠があれば、HP由来情報をリンクして重複作成しない
    existing_manual = await _find_existing_manual_match(
        db,
        patient_id=patient.id,
        start_time=parsed["start_time"],
        end_time=parsed["end_time"],
    )
    if existing_manual:
        existing_manual.source_ref = parsed["reservation_number"]
        existing_manual.hotpepper_synced = True
        existing_manual.color_id = hotpepper_color_id
        existing_manual.notes = (existing_manual.notes or "") + " / HPメール照合: 手動登録済み予約にリンク"

        await create_notification(
            db,
            "hotpepper_linked_existing",
            f"HotPepper照合: 既存予約に紐付け {parsed['patient_name']} "
            f"{parsed['start_time'].strftime('%m/%d %H:%M')}-{parsed['end_time'].strftime('%H:%M')}",
            existing_manual.id,
        )

        await db.commit()
        return {
            "status": "skipped",
            "reason": "linked_existing_manual",
            "reservation_id": existing_manual.id,
            "reservation_number": parsed["reservation_number"],
        }

    # ── メニュー解決 & 施術時間スナップ ──
    hp_menu_id, snapped_duration = await _resolve_hotpepper_menu(db, parsed["duration_minutes"])
    if snapped_duration != parsed["duration_minutes"]:
        parsed["end_time"] = parsed["start_time"] + timedelta(minutes=snapped_duration)
        parsed["duration_minutes"] = snapped_duration

    menu_note = f"HPメニュー名: {parsed['menu_name']}" if parsed.get("menu_name") else None
    practitioner_id, prac_note = await _assign_practitioner(
        db,
        parsed.get("practitioner_name"),
        parsed["start_time"],
        parsed["end_time"],
    )

    notes = _build_notes(parsed, menu_note, prac_note)

    reservation = Reservation(
        patient_id=patient.id,
        practitioner_id=practitioner_id,
        menu_id=hp_menu_id,
        color_id=hotpepper_color_id,
        start_time=parsed["start_time"],
        end_time=parsed["end_time"],
        status="CONFIRMED",
        channel="HOTPEPPER",
        source_ref=parsed["reservation_number"],
        notes=notes,
        hotpepper_synced=True,
    )
    db.add(reservation)
    await db.flush()

    await _notify_hotpepper_conflict_risk(
        db=db,
        parsed=parsed,
        reservation_id=reservation.id,
        practitioner_id=practitioner_id,
        start_time=parsed["start_time"],
        end_time=parsed["end_time"],
        practitioner_note=prac_note,
    )

    logger.info(f"予約作成: id={reservation.id}, source_ref={parsed['reservation_number']}")

    await create_notification(
        db,
        "new_reservation",
        f"HotPepper予約: {parsed['patient_name']} "
        f"{parsed['start_time'].strftime('%m/%d %H:%M')}-{parsed['end_time'].strftime('%H:%M')} "
        f"{parsed.get('menu_name') or '(メニュー不明)'}",
        reservation.id,
        extra_data={"channel": "HOTPEPPER"},
    )

    await db.commit()
    return {
        "status": "created",
        "reservation_id": reservation.id,
        "reservation_number": parsed["reservation_number"],
    }


async def _handle_cancelled(db: AsyncSession, parsed: dict) -> dict:
    """既存予約のキャンセル処理"""
    ref = parsed["reservation_number"]

    result = await db.execute(
        select(Reservation).where(Reservation.source_ref == ref)
    )
    reservation = result.scalar_one_or_none()

    if not reservation:
        logger.warning(f"キャンセル対象の予約が見つかりません: source_ref={ref}")
        return {"status": "skipped", "reason": "not_found", "reservation_number": ref}

    if reservation.status == "CANCELLED":
        logger.info(f"既にキャンセル済み: source_ref={ref}")
        return {"status": "skipped", "reason": "already_cancelled", "reservation_number": ref}

    old_status = reservation.status
    reservation.status = "CANCELLED"
    reservation.notes = (reservation.notes or "") + " / HPキャンセル通知により自動キャンセル"

    logger.info(f"予約キャンセル: id={reservation.id}, {old_status}→CANCELLED, source_ref={ref}")

    await create_notification(
        db,
        "reservation_cancelled",
        f"HotPepperキャンセル: {parsed['patient_name']} "
        f"{parsed['start_time'].strftime('%m/%d %H:%M')} "
        f"{parsed.get('menu_name') or ''}",
        reservation.id,
    )

    await db.commit()
    return {
        "status": "cancelled",
        "reservation_id": reservation.id,
        "reservation_number": ref,
    }


async def _handle_changed(db: AsyncSession, parsed: dict) -> dict:
    """既存予約の変更処理"""
    ref = parsed["reservation_number"]

    result = await db.execute(
        select(Reservation).where(Reservation.source_ref == ref)
    )
    reservation = result.scalar_one_or_none()

    if not reservation:
        # 変更通知だが元予約が未登録 → 新規として登録
        logger.info(f"変更対象が未登録のため新規作成: source_ref={ref}")
        return await _handle_created(db, parsed)

    # 変更内容を更新
    # ── メニュー解決 & 施術時間スナップ ──
    hp_menu_id, snapped_duration = await _resolve_hotpepper_menu(db, parsed["duration_minutes"])
    if snapped_duration != parsed["duration_minutes"]:
        parsed["end_time"] = parsed["start_time"] + timedelta(minutes=snapped_duration)
        parsed["duration_minutes"] = snapped_duration

    reservation.start_time = parsed["start_time"]
    reservation.end_time = parsed["end_time"]
    reservation.color_id = await _resolve_hotpepper_color_id(db)
    reservation.menu_id = hp_menu_id

    menu_note = f"HPメニュー名: {parsed['menu_name']}" if parsed.get("menu_name") else None

    practitioner_id, prac_note = await _assign_practitioner(
        db,
        parsed.get("practitioner_name"),
        parsed["start_time"],
        parsed["end_time"],
    )
    reservation.practitioner_id = practitioner_id

    notes = _build_notes(parsed, menu_note, prac_note, prefix="HotPepper変更予約")
    reservation.notes = notes

    await _notify_hotpepper_conflict_risk(
        db=db,
        parsed=parsed,
        reservation_id=reservation.id,
        practitioner_id=practitioner_id,
        start_time=parsed["start_time"],
        end_time=parsed["end_time"],
        practitioner_note=prac_note,
    )

    logger.info(f"予約変更: id={reservation.id}, source_ref={ref}")

    await create_notification(
        db,
        "reservation_changed",
        f"HotPepper変更: {parsed['patient_name']} "
        f"{parsed['start_time'].strftime('%m/%d %H:%M')}-{parsed['end_time'].strftime('%H:%M')} "
        f"{parsed.get('menu_name') or ''}",
        reservation.id,
    )

    await db.commit()
    return {
        "status": "changed",
        "reservation_id": reservation.id,
        "reservation_number": ref,
    }


def _build_notes(parsed: dict, menu_note: str | None, prac_note: str | None,
                 prefix: str = "HotPepper予約") -> str:
    """備考文字列を組み立て"""
    parts = [prefix]
    if parsed.get("coupon_name"):
        parts.append(f"クーポン: {parsed['coupon_name']}")
    if parsed.get("note"):
        parts.append(f"要望: {parsed['note']}")
    if parsed.get("amount") is not None:
        parts.append(f"金額: {parsed['amount']}円")
    if menu_note:
        parts.append(menu_note)
    if prac_note:
        parts.append(prac_note)
    return " / ".join(parts)


async def _notify_hotpepper_conflict_risk(
    db: AsyncSession,
    parsed: dict,
    reservation_id: int,
    practitioner_id: int,
    start_time: datetime,
    end_time: datetime,
    practitioner_note: str | None,
):
    """予約バッティングの可能性がある場合に通知を作成する。"""
    conflicts = await check_conflict(db, practitioner_id, start_time, end_time)
    has_conflict = len(conflicts) > 0
    fallback_warning = bool(practitioner_note and ("空きがない" in practitioner_note or "空き判定" in practitioner_note))

    if not has_conflict and not fallback_warning:
        return

    detail_parts: list[str] = []
    if has_conflict:
        detail_parts.append(f"重複候補{len(conflicts)}件")
    if fallback_warning:
        detail_parts.append(practitioner_note)

    await create_notification(
        db,
        "hotpepper_conflict",
        f"予約バッティング注意: {parsed.get('patient_name') or '(氏名不明)'} "
        f"{start_time.strftime('%m/%d %H:%M')}-{end_time.strftime('%H:%M')} "
        f"({', '.join(detail_parts)})",
        reservation_id,
    )


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------


async def _find_or_create_patient(db: AsyncSession, name: str, reading: str | None = None) -> Patient:
    """患者を名前で検索（チャネル横断マッチング）。見つからなければ新規作成。"""
    from app.services.patient_match import find_or_create_patient
    return await find_or_create_patient(db, name=name, reading=reading)


async def _get_or_create_hotpepper_dummy_patient(db: AsyncSession) -> Patient:
    """シャドーモード専用: ホットペッパーN のダミー患者を新規作成して返す。

    毎回新規（HP予約番号単位で別患者）。既存の「ホットペッパーN」の最大番号を探してインクリメント。
    本番では shadow_mode=False のため絶対に呼ばれない。
    """
    import re
    from app.services.patient_match import create_new_patient

    existing_result = await db.execute(
        select(Patient).where(Patient.name.like("ホットペッパー%"))
    )
    existing = existing_result.scalars().all()
    max_number = 0
    for p in existing:
        m = re.fullmatch(r"ホットペッパー(\d+)", p.name or "")
        if m:
            max_number = max(max_number, int(m.group(1)))
    alias_name = f"ホットペッパー{max_number + 1}"
    patient = await create_new_patient(db, name=alias_name, line_id=None)
    logger.info(f"[shadow] HPダミー患者作成: {alias_name}")
    return patient


async def _find_existing_manual_match(
    db: AsyncSession,
    patient_id: int,
    start_time: datetime,
    end_time: datetime,
) -> Reservation | None:
    """同一患者・同一時間枠の既存予約を検索（HOTPEPPER未連携の手動予約優先）。"""
    result = await db.execute(
        select(Reservation)
        .where(
            Reservation.patient_id == patient_id,
            Reservation.start_time == start_time,
            Reservation.end_time == end_time,
            Reservation.status.in_(["CONFIRMED", "PENDING", "HOLD"]),
            Reservation.source_ref.is_(None),
        )
        .order_by(Reservation.id.desc())
    )
    return result.scalar_one_or_none()


async def _resolve_hotpepper_color_id(db: AsyncSession) -> Optional[int]:
    """HotPepper予約に固定適用する色IDを返す。未設定時は None。"""
    result = await db.execute(
        select(ReservationColor).where(ReservationColor.color_code == HOTPEPPER_FIXED_COLOR_CODE)
    )
    color = result.scalar_one_or_none()
    if color:
        return color.id
    logger.warning("HotPepper固定色が見つかりません: %s", HOTPEPPER_FIXED_COLOR_CODE)
    return None


async def _resolve_hotpepper_menu(
    db: AsyncSession, duration_minutes: int
) -> tuple[Optional[int], int]:
    """HotPepper専用メニュー（"ホットペッパー"）を検索し、price_tier から最適な施術時間を返す。

    Returns:
        (menu_id or None, snapped_duration_minutes)
        メニューが見つからない場合は (None, 元の duration_minutes) をそのまま返す。
    """
    from sqlalchemy.orm import selectinload

    result = await db.execute(
        select(Menu)
        .options(selectinload(Menu.price_tiers))
        .where(Menu.name == HOTPEPPER_MENU_NAME, Menu.is_active == True)
    )
    menu = result.unique().scalar_one_or_none()
    if not menu:
        logger.warning("HotPepper専用メニュー '%s' が見つかりません", HOTPEPPER_MENU_NAME)
        return None, duration_minutes

    # price_tiers がある場合、最も近い tier の duration にスナップ
    # ただし差が 15分超の場合は誤スナップを防ぎパース値をそのまま使う
    if menu.price_tiers:
        tier_durations = [t.duration_minutes for t in menu.price_tiers]
        best = min(tier_durations, key=lambda d: abs(d - duration_minutes))
        if abs(best - duration_minutes) > 15:
            logger.info(
                "HotPepperメニュー duration snap スキップ(差 %d分): %d分のまま使用 (tiers=%s)",
                abs(best - duration_minutes), duration_minutes, tier_durations,
            )
            return menu.id, duration_minutes
        logger.info(
            "HotPepperメニュー duration snap: %d分 → %d分 (tiers=%s)",
            duration_minutes, best, tier_durations,
        )
        return menu.id, best

    return menu.id, duration_minutes


async def _match_menu(db: AsyncSession, menu_name: Optional[str]) -> tuple[Optional[int], Optional[int], Optional[str]]:
    """メニュー名でシステム内メニューを検索。
    Returns: (menu_id or None, menu_color_id or None, 不一致時の注記 or None)
    """
    if not menu_name:
        return None, None, None

    # 完全一致
    result = await db.execute(
        select(Menu).where(Menu.name == menu_name, Menu.is_active == True)
    )
    menu = result.unique().scalar_one_or_none()
    if menu:
        return menu.id, menu.color_id, None

    # 部分一致: メニュー名がシステム側に含まれるか
    result = await db.execute(
        select(Menu).where(Menu.is_active == True)
    )
    menus = result.unique().scalars().all()
    for m in menus:
        if m.name in menu_name or menu_name in m.name:
            return m.id, m.color_id, None

    return None, None, f"HPメニュー名: {menu_name}"


async def _is_practitioner_available(
    db: AsyncSession,
    practitioner_id: int,
    start_time: datetime,
    end_time: datetime,
) -> bool:
    target_date = start_time.date()
    working, _, _ = await is_practitioner_working(db, practitioner_id, target_date)
    if not working:
        return False

    # 時間帯休み
    uts = (
        await db.execute(
            select(PractitionerUnavailableTime).where(
                and_(
                    PractitionerUnavailableTime.practitioner_id == practitioner_id,
                    PractitionerUnavailableTime.date == target_date,
                )
            )
        )
    ).scalars().all()
    s_min = start_time.hour * 60 + start_time.minute
    e_min = end_time.hour * 60 + end_time.minute
    for ut in uts:
        sh, sm = map(int, ut.start_time.split(":"))
        eh, em = map(int, ut.end_time.split(":"))
        ut_s = sh * 60 + sm
        ut_e = eh * 60 + em
        if s_min < ut_e and e_min > ut_s:
            return False

    conflicts = await check_conflict(db, practitioner_id, start_time, end_time)
    return len(conflicts) == 0


def _priority_of_role(role: str | None) -> int:
    if role == "施術者":
        return 0
    if role == "院長":
        return 1
    return 9


async def _assign_practitioner(
    db: AsyncSession,
    name: Optional[str],
    start_time: datetime,
    end_time: datetime,
) -> tuple[int, Optional[str]]:
    """施術者を割当。希望なし時は 施術者→院長 の優先で空きに割当。
    Returns: (practitioner_id, 注記 or None)
    """
    note = None

    if name:
        result = await db.execute(
            select(Practitioner).where(Practitioner.name == name, Practitioner.is_active == True)
        )
        prac = result.scalar_one_or_none()
        if prac:
            if await _is_practitioner_available(db, prac.id, start_time, end_time):
                return prac.id, None
            note = f"指名「{name}」は空きがないため優先順位割当に変更"
        else:
            note = f"指名「{name}」が見つからずデフォルト割当"
        logger.warning(note)

    # 希望なし時の優先順位: 施術者 → 院長（その中で display_order昇順）
    result = await db.execute(
        select(Practitioner).where(Practitioner.is_active == True).order_by(Practitioner.display_order, Practitioner.id)
    )
    practitioners = result.scalars().all()

    preferred_order = sorted(practitioners, key=lambda p: (_priority_of_role(p.role), p.display_order, p.id))
    for p in preferred_order:
        if await _is_practitioner_available(db, p.id, start_time, end_time):
            if not name and p.role == "院長":
                note = (note + " / " if note else "") + "施術者枠が埋まっているため院長に割当"
            return p.id, note

    # 全員埋まり時は優先順位の先頭へ（空き判定失敗時の最終フォールバック）
    if preferred_order:
        note = (note + " / " if note else "") + "全員の空き判定が取れず優先順位先頭へ割当"
        return preferred_order[0].id, note

    raise ValueError("アクティブな施術者が登録されていません")
