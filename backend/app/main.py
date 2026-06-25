from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api import practitioners, patients, menus, settings, notifications, sse, reservations, hotpepper, line, auth, reservation_colors, chatbot, weekly_schedules, practitioner_schedules, patient_import, date_overrides, business_hours, web_reserve, public, shadow_logs, audit_logs
from app.services.hold_expiration import start_hold_expiration_job, stop_hold_expiration_job
from app.database import async_session
from app.services.bootstrap import initialize_database
from app.services.line_alerts import push_developer_sos_alert

FIELD_LABELS = {
    "practitioner_id": "施術者",
    "patient_id": "患者",
    "menu_id": "メニュー",
    "start_time": "開始時間",
    "end_time": "終了時間",
    "channel": "チャネル",
    "name": "名前",
    "duration_minutes": "施術時間",
    "price": "料金",
    "color_code": "色コード",
    "notes": "備考",
    "date": "日付",
    "phone": "電話番号",
    "email": "メールアドレス",
    "role": "役割",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await initialize_database()
        async with async_session() as db:
            await db.execute(text("SELECT 1"))
    except Exception as e:
        await push_developer_sos_alert(
            "アプリ起動時のDB接続に失敗しました",
            detail=str(e),
            source="startup_db_check",
            error_type=type(e).__name__,
            failure_streak=1,
            dedupe_key="startup_db_check",
            min_interval_seconds=60,
        )
        raise

    start_hold_expiration_job()
    yield
    stop_hold_expiration_job()


from app.config import settings as app_settings_early

app = FastAPI(title=app_settings_early.app_title, version="1.0.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    messages = []
    for err in exc.errors():
        field = err.get("loc", [])[-1] if err.get("loc") else "不明"
        label = FIELD_LABELS.get(str(field), str(field))
        msg = err.get("msg", "")
        # Pydantic V2 messages: "Value error, ..." -> strip prefix
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        messages.append(f"{label}: {msg}")
    return JSONResponse(
        status_code=422,
        content={"detail": " / ".join(messages)},
    )

from app.config import settings as app_settings

_cors_origins = ["http://localhost:5173"]
if app_settings.cors_origins:
    _cors_origins.extend(
        o.strip() for o in app_settings.cors_origins.split(",") if o.strip()
    )
if app_settings.chatbot_allowed_origins:
    _cors_origins.extend(
        o.strip() for o in app_settings.chatbot_allowed_origins.split(",") if o.strip()
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# /api/hotpepper/* への呼び出しを rpa_call_logs に観測記録（監査ログとは別系統）
from app.middlewares.rpa_call_log import RpaCallLogMiddleware  # noqa: E402

app.add_middleware(RpaCallLogMiddleware)

app.include_router(practitioners.router)
app.include_router(patients.router)
app.include_router(menus.router)
app.include_router(settings.router)
app.include_router(notifications.router)
app.include_router(sse.router)
app.include_router(reservations.router)
app.include_router(hotpepper.router)
app.include_router(line.router)
app.include_router(auth.router)
app.include_router(reservation_colors.router)
app.include_router(chatbot.router)
app.include_router(weekly_schedules.router)
app.include_router(practitioner_schedules.router)
app.include_router(patient_import.router)
app.include_router(date_overrides.router)
app.include_router(business_hours.router)
app.include_router(web_reserve.router)
app.include_router(public.router)
app.include_router(shadow_logs.router)
app.include_router(audit_logs.router)


@app.get("/")
async def root():
    return {"message": f"{app_settings_early.app_title} API"}


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "ok"}
