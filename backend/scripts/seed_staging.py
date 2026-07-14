"""ステージング環境向けシードスクリプト
管理者設定 + 施術者3名 + メニュー4種 + 患者5名 + 予約10件(過去5+未来5)を投入。
冪等: source_ref='STG-XXX' が既に存在すればスキップ / マスターデータは upsert。

使い方 (Docker 経由):
  docker-compose exec -e DATABASE_URL="postgresql+asyncpg://...@.../coco_staging" \
      backend python scripts/seed_staging.py
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import zoneinfo
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.models.practitioner import Practitioner
from app.models.patient import Patient
from app.models.menu import Menu
from app.models.reservation_color import ReservationColor
from app.models.reservation import Reservation
from app.models.reservation_series import ReservationSeries  # noqa: F401 — relationship解決用
from app.models.setting import Setting
from app.database import Base

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ---------------------------------------------------------------------------
# データ定義
# ---------------------------------------------------------------------------

# 管理者パスワード（ログインテスト用）
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "staging1234"  # ステージング専用の固定パスワード

PRACTITIONERS = [
    {"name": "田中 太郎", "role": "院長",   "display_order": 1},
    {"name": "鈴木 花子", "role": "施術者", "display_order": 2},
    {"name": "佐藤 健二", "role": "施術者", "display_order": 3},
]

COLORS = [
    {"name": "保険診療",               "color_code": "#3B82F6", "display_order": 1, "is_default": True},
    {"name": "自費診療",               "color_code": "#10B981", "display_order": 2, "is_default": False},
    {"name": "初診／ホットペッパー予約", "color_code": "#F97316", "display_order": 3, "is_default": False},
]

# (name, duration_minutes, price, color_name, display_order)
MENUS = [
    ("骨盤矯正",       45, 5000, "自費診療",               1),
    ("全身マッサージ",  60, 6000, "自費診療",               2),
    ("鍼灸コース",      30, 3500, "自費診療",               3),
    ("保険診療３割負担", 15,  900, "保険診療",               4),
]

PATIENTS = [
    {"name": "山田 一郎",   "last_name": "山田",  "first_name": "一郎",
     "last_name_kana": "ヤマダ",   "first_name_kana": "イチロウ", "patient_number": "S000001"},
    {"name": "田中 美咲",   "last_name": "田中",  "first_name": "美咲",
     "last_name_kana": "タナカ",   "first_name_kana": "ミサキ",   "patient_number": "S000002"},
    {"name": "中村 健太",   "last_name": "中村",  "first_name": "健太",
     "last_name_kana": "ナカムラ", "first_name_kana": "ケンタ",   "patient_number": "S000003"},
    {"name": "佐藤 陽子",   "last_name": "佐藤",  "first_name": "陽子",
     "last_name_kana": "サトウ",   "first_name_kana": "ヨウコ",   "patient_number": "S000004"},
    {"name": "小林 浩二",   "last_name": "小林",  "first_name": "浩二",
     "last_name_kana": "コバヤシ", "first_name_kana": "コウジ",   "patient_number": "S000005"},
]

# ---------------------------------------------------------------------------
# 初期設定 (bootstrap と同等 + ステージング用パスワード)
# ---------------------------------------------------------------------------
def _staging_settings() -> list[tuple[str, str]]:
    return [
        ("hold_duration_minutes", "10"),
        ("hotpepper_priority", "true"),
        ("business_hour_start", "09:00"),
        ("business_hour_end", "20:00"),
        ("business_days", "1,2,3,4,5,6"),
        ("slot_interval_minutes", "5"),
        ("notification_sound", "true"),
        ("notification_sound_hotpepper", "school_chime"),
        ("notification_sound_line", "triple_bell"),
        ("notification_sound_web", "bright_ascend"),
        ("chatbot_enabled", "true"),
        ("chatbot_accept_start", "00:00"),
        ("chatbot_accept_end", "23:59"),
        ("chatbot_greeting", "こんにちは！ご予約のお手伝いをいたします。\nご希望の日時やメニューをお聞かせください。"),
        ("chatbot_confirm_message", "当日のご来院をお待ちしております。\nご変更・キャンセルはお電話にてお願いいたします。"),
        ("chatbot_system_prompt", "あなたは予約受付アシスタントです。"),
        ("chatbot_disabled_message", "申し訳ございません。現在チャットボット機能は準備中です。"),
        ("line_reply_reservation", "ご予約のご連絡ありがとうございます。"),
        ("line_reply_default", "メッセージを受け付けました。"),
        ("practitioner_roles", "院長,施術者"),
        ("holiday_mode", "closed"),
        ("holiday_start_time", "09:00"),
        ("holiday_end_time", "13:00"),
        ("admin_username", ADMIN_USERNAME),
        ("admin_password_hash", pwd_context.hash(ADMIN_PASSWORD)),
    ]


# ---------------------------------------------------------------------------
# 予約データ (過去5件 + 未来5件 = 10件)
# ---------------------------------------------------------------------------
def build_reservations() -> list[dict]:
    today = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    return [
        # ── 過去の予約 (完了済み) ──
        {"ref": "STG-001", "p": 0, "pt": 0, "m": 0,  # 骨盤矯正 45分
         "s": today - timedelta(days=3, hours=-10),
         "e": today - timedelta(days=3, hours=-10, minutes=-45),
         "status": "CONFIRMED", "channel": "PHONE"},
        {"ref": "STG-002", "p": 1, "pt": 1, "m": 1,  # 全身マッサージ 60分
         "s": today - timedelta(days=3, hours=-14),
         "e": today - timedelta(days=3, hours=-15),
         "status": "CONFIRMED", "channel": "LINE"},
        {"ref": "STG-003", "p": 2, "pt": 2, "m": 2,  # 鍼灸コース 30分
         "s": today - timedelta(days=2, hours=-10),
         "e": today - timedelta(days=2, hours=-10, minutes=-30),
         "status": "CONFIRMED", "channel": "WALK_IN"},
        {"ref": "STG-004", "p": 0, "pt": 3, "m": 3,  # 保険診療 15分
         "s": today - timedelta(days=2, hours=-15),
         "e": today - timedelta(days=2, hours=-15, minutes=-15),
         "status": "CONFIRMED", "channel": "HOTPEPPER",
         "hotpepper_synced": True},
        {"ref": "STG-005", "p": 1, "pt": 4, "m": 0,  # 骨盤矯正 45分
         "s": today - timedelta(days=1, hours=-11),
         "e": today - timedelta(days=1, hours=-11, minutes=-45),
         "status": "CONFIRMED", "channel": "PHONE"},

        # ── 未来の予約 ──
        {"ref": "STG-006", "p": 0, "pt": 0, "m": 1,  # 全身マッサージ 60分 (明日)
         "s": today + timedelta(days=1, hours=10),
         "e": today + timedelta(days=1, hours=11),
         "status": "CONFIRMED", "channel": "LINE"},
        {"ref": "STG-007", "p": 1, "pt": 1, "m": 2,  # 鍼灸コース 30分 (明日)
         "s": today + timedelta(days=1, hours=14),
         "e": today + timedelta(days=1, hours=14, minutes=30),
         "status": "PENDING",   "channel": "CHATBOT"},
        {"ref": "STG-008", "p": 2, "pt": 2, "m": 0,  # 骨盤矯正 45分 (明後日)
         "s": today + timedelta(days=2, hours=10),
         "e": today + timedelta(days=2, hours=10, minutes=45),
         "status": "CONFIRMED", "channel": "PHONE"},
        {"ref": "STG-009", "p": 0, "pt": 3, "m": 3,  # 保険診療 15分 (明後日)
         "s": today + timedelta(days=2, hours=16),
         "e": today + timedelta(days=2, hours=16, minutes=15),
         "status": "PENDING",   "channel": "WALK_IN"},
        {"ref": "STG-010", "p": 1, "pt": 4, "m": 1,  # 全身マッサージ 60分 (3日後)
         "s": today + timedelta(days=3, hours=13),
         "e": today + timedelta(days=3, hours=14),
         "status": "CONFIRMED", "channel": "HOTPEPPER",
         "hotpepper_synced": True},
    ]


# ---------------------------------------------------------------------------
# ヘルパー (upsert)
# ---------------------------------------------------------------------------
async def upsert_setting(db: AsyncSession, key: str, value: str) -> None:
    result = await db.execute(select(Setting).where(Setting.key == key))
    obj = result.scalar_one_or_none()
    if obj:
        obj.value = value
    else:
        db.add(Setting(key=key, value=value))


async def get_or_create_practitioner(db: AsyncSession, d: dict) -> Practitioner:
    r = await db.execute(select(Practitioner).where(Practitioner.name == d["name"]))
    obj = r.scalar_one_or_none()
    if obj:
        print(f"  既存: 施術者 '{d['name']}'")
        return obj
    obj = Practitioner(**d, is_active=True, is_visible=True)
    db.add(obj)
    await db.flush()
    print(f"  作成: 施術者 '{d['name']}'")
    return obj


async def get_or_create_color(db: AsyncSession, d: dict) -> ReservationColor:
    r = await db.execute(select(ReservationColor).where(ReservationColor.name == d["name"]))
    obj = r.scalar_one_or_none()
    if obj:
        print(f"  既存: 予約色 '{d['name']}'")
        return obj
    obj = ReservationColor(**d)
    db.add(obj)
    await db.flush()
    print(f"  作成: 予約色 '{d['name']}'")
    return obj


async def upsert_menu(db: AsyncSession, row: tuple, color_map: dict[str, int]) -> Menu:
    name, duration, price, color_name, order = row
    color_id = color_map.get(color_name)
    r = await db.execute(select(Menu).where(Menu.name == name))
    obj = r.unique().scalar_one_or_none()
    if obj:
        obj.duration_minutes = duration
        obj.price = price
        obj.color_id = color_id
        obj.display_order = order
        obj.is_active = True
        await db.flush()
        print(f"  更新: メニュー '{name}'")
        return obj
    obj = Menu(
        name=name, duration_minutes=duration, price=price,
        color_id=color_id, display_order=order, is_active=True,
    )
    db.add(obj)
    await db.flush()
    print(f"  作成: メニュー '{name}'")
    return obj


async def get_or_create_patient(db: AsyncSession, d: dict) -> Patient:
    r = await db.execute(select(Patient).where(Patient.patient_number == d["patient_number"]))
    obj = r.scalar_one_or_none()
    if obj:
        print(f"  既存: 患者 '{d['name']}'")
        return obj
    obj = Patient(**d)
    db.add(obj)
    await db.flush()
    print(f"  作成: 患者 '{d['name']}'")
    return obj


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------
async def seed_staging():
    # DATABASE_URL 環境変数から接続（Docker 経由で上書き想定）
    from app.config import settings
    db_url = settings.database_url
    print(f"接続先: {db_url[:db_url.index('@') + 1]}***")

    engine = create_async_engine(db_url, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # テーブルが無ければ作成 (alembic を通さない簡易セットアップ)
    from app.services.bootstrap import _import_all_models
    _import_all_models()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("テーブル確認/作成 完了")

    async with session_factory() as db:
        # ── 初期設定 (admin 含む) ──
        print("\n=== 初期設定 ===")
        for key, value in _staging_settings():
            await upsert_setting(db, key, value)
        await db.commit()
        print(f"  設定 {len(_staging_settings())} 件を投入/更新しました")
        print(f"  ログイン情報: username={ADMIN_USERNAME} / password={ADMIN_PASSWORD}")

        # ── 施術者 ──
        print("\n=== 施術者 ===")
        practitioners = [await get_or_create_practitioner(db, d) for d in PRACTITIONERS]
        await db.commit()
        print(f"  施術者 {len(practitioners)} 件を確認しました")

        # ── 予約色 ──
        print("\n=== 予約色 ===")
        color_objs = [await get_or_create_color(db, d) for d in COLORS]
        await db.flush()
        color_map = {c.name: c.id for c in color_objs}
        await db.commit()

        # ── メニュー ──
        print("\n=== メニュー ===")
        menus = [await upsert_menu(db, row, color_map) for row in MENUS]
        await db.commit()
        print(f"  メニュー {len(menus)} 件を確認しました")

        # ── 患者 ──
        print("\n=== 患者 ===")
        patients = [await get_or_create_patient(db, d) for d in PATIENTS]
        await db.commit()
        print(f"  患者 {len(patients)} 件を確認しました")

        # ── 予約 ──
        print("\n=== 予約 (過去5件 + 未来5件) ===")
        rdata = build_reservations()
        added = 0
        skipped = 0
        for rd in rdata:
            r = await db.execute(
                select(Reservation).where(Reservation.source_ref == rd["ref"])
            )
            if r.scalar_one_or_none():
                print(f"  既存: {rd['ref']}")
                skipped += 1
                continue

            reservation = Reservation(
                patient_id=patients[rd["pt"]].id,
                practitioner_id=practitioners[rd["p"]].id,
                menu_id=menus[rd["m"]].id,
                start_time=rd["s"],
                end_time=rd["e"],
                status=rd["status"],
                channel=rd["channel"],
                source_ref=rd["ref"],
                hotpepper_synced=rd.get("hotpepper_synced", False),
            )
            db.add(reservation)
            try:
                await db.flush()
                label = "過去" if rd["s"] < datetime.now(JST) else "未来"
                print(f"  作成: {rd['ref']} [{label}] {rd['s'].strftime('%m/%d %H:%M')}-{rd['e'].strftime('%H:%M')} {rd['status']}")
                added += 1
            except Exception as exc:
                await db.rollback()
                print(f"  ERROR: {rd['ref']} — {exc}")

        await db.commit()

    await engine.dispose()
    print(f"\n✅ シード完了: 予約 追加={added}, スキップ={skipped}")
    print(f"   ログイン → username: {ADMIN_USERNAME} / password: {ADMIN_PASSWORD}")


if __name__ == "__main__":
    asyncio.run(seed_staging())
