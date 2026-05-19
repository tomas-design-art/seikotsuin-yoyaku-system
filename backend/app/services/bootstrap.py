from importlib import import_module

from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.setting import Setting

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _import_all_models() -> None:
    # Keep model imports explicit so runtime checks fail early on mapping errors.
    model_modules = [
        "app.models.audit_log",
        "app.models.chat_session",
        "app.models.date_override",
        "app.models.line_user_state",
        "app.models.menu",
        "app.models.notification_log",
        "app.models.patient",
        "app.models.practitioner",
        "app.models.practitioner_schedule",
        "app.models.practitioner_unavailable_time",
        "app.models.reservation",
        "app.models.reservation_color",
        "app.models.reservation_series",
        "app.models.setting",
        "app.models.shadow_log",
        "app.models.weekly_schedule",
    ]
    for module_path in model_modules:
        import_module(module_path)


def _initial_settings() -> list[tuple[str, str]]:
    return [
        ("hold_duration_minutes", "10"),
        ("hotpepper_priority", "true"),
        ("business_hour_start", "09:00"),
        ("business_hour_end", "20:00"),
        ("business_days", "1,2,3,4,5,6"),
        ("slot_interval_minutes", "5"),
        ("notification_sound", "true"),
        ("chatbot_enabled", "true"),
        ("chatbot_accept_start", "00:00"),
        ("chatbot_accept_end", "23:59"),
        ("chatbot_greeting", "こんにちは！ご予約のお手伝いをいたします。\nご希望の日時やメニューをお聞かせください。"),
        ("chatbot_confirm_message", "当日のご来院をお待ちしております。\nご変更・キャンセルはお電話にてお願いいたします。"),
        ("chatbot_system_prompt", "あなたは予約受付アシスタントです。\n患者さんと丁寧に会話しながら、予約を受け付けてください。\n\nルール:\n1. 予約に必要な情報を会話で収集する:\n   - 希望日時\n   - 施術メニュー（メニュー一覧から選択）\n   - 患者名\n   - 電話番号\n2. 情報が揃ったら空き状況を確認する\n3. 空いていれば予約を確定する\n4. 空いていなければ代替候補を最大3つ提案する\n5. 敬語で丁寧に対応する\n6. 予約に関係ない質問には「お電話でお問い合わせください」と案内する"),
        ("chatbot_disabled_message", "申し訳ございません。現在チャットボット機能は準備中です。お電話にてお問い合わせください。"),
        ("line_reply_reservation", "ご予約のご連絡ありがとうございます。\nご希望の日時を確認し、折り返しご連絡いたします。"),
        ("line_reply_default", "メッセージを受け付けました。内容を確認いたします。"),
        ("practitioner_roles", "院長,施術者"),
        ("holiday_mode", "closed"),
        ("holiday_start_time", "09:00"),
        ("holiday_end_time", "13:00"),
        ("admin_username", "admin"),
        ("admin_password_hash", pwd_context.hash("admin")),
    ]


async def seed_initial_settings(db: AsyncSession) -> None:
    for key, value in _initial_settings():
        result = await db.execute(select(Setting).where(Setting.key == key))
        existing = result.scalar_one_or_none()
        if existing is None:
            db.add(Setting(key=key, value=value))
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()


async def initialize_database() -> None:
    _import_all_models()
    async with async_session() as db:
        await seed_initial_settings(db)
