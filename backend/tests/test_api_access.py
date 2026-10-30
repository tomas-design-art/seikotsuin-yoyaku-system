"""API の入口の鍵（画面サーバー経由・オーナーの判断 2026-10-30）。

API は、スタッフの画面を配るサーバー（入口のログインの後ろ）が中継したときだけ通す。
そのサーバーは合言葉（X-Internal-Key）を付けて呼ぶ。ほかに外から呼ぶのは次の4つだけで、
それぞれ自分の合言葉で、決めた口にだけ通す。

- ホームページのサーバー：X-Homepage-Key。空き枠・公開メニュー・ホームページの予約だけ。
- HotPepper の RPA：Authorization: Bearer <RPA の合言葉>。RPA が使う3つの口だけ。
- 日計表のツール：Authorization: Bearer <日計表の合言葉>。予約の一覧を読むこと（GET）だけ。
  RPA も日計表のツールも、もともと設定ファイルの値を Bearer で送る作り＝プログラムは直さず、
  設定の値を1つ入れるだけで済む（オーナーの判断 2026-10-31：ローカルで作業が要る変更は困る）。
- LINE：Webhook（署名で確かめる）。合言葉は要らない。

切り替えで止まらないよう、強さは3段階（open＝今まで通り／report＝通すが記録する／
enforce＝断る）。既定は open。
"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.config import settings as app_settings
from app.middlewares.api_access import ApiAccessMiddleware, decide

INTERNAL = "internal-key-for-tests"
HOMEPAGE = "homepage-key-for-tests"
RPA = "rpa-key-for-tests"
DAILY = "daily-report-key-for-tests"


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(app_settings, "internal_api_key", INTERNAL, raising=False)
    monkeypatch.setattr(app_settings, "homepage_api_key", HOMEPAGE, raising=False)
    monkeypatch.setattr(app_settings, "rpa_api_key", RPA, raising=False)
    monkeypatch.setattr(app_settings, "daily_report_api_key", DAILY, raising=False)


def _decide(method: str, path: str, headers: dict[str, str] | None = None) -> bool:
    return decide(method, path, {k.lower(): v for k, v in (headers or {}).items()}, app_settings)


# ─── どの呼び出しを通すか ───


def test_health_and_line_webhook_need_no_key(keys):
    assert _decide("GET", "/health")
    assert _decide("HEAD", "/health")
    assert _decide("GET", "/")
    assert _decide("POST", "/api/line/webhook")


def test_without_a_key_the_api_is_closed(keys):
    assert not _decide("GET", "/api/reservations/")
    assert not _decide("GET", "/api/settings/")
    assert not _decide("GET", "/api/sse/events")
    assert not _decide("POST", "/api/hotpepper/receive-email")
    assert not _decide("GET", "/docs")
    assert not _decide("GET", "/openapi.json")


def test_the_staff_screen_server_key_opens_the_whole_api(keys):
    staff = {"X-Internal-Key": INTERNAL}
    assert _decide("GET", "/api/reservations/", staff)
    assert _decide("PUT", "/api/reservations/12", staff)
    assert _decide("GET", "/api/sse/events", staff)
    assert _decide("GET", "/api/public/menus", staff)


def test_a_wrong_or_empty_key_does_not_open(keys):
    assert not _decide("GET", "/api/reservations/", {"X-Internal-Key": "wrong"})
    assert not _decide("GET", "/api/reservations/", {"X-Internal-Key": ""})
    assert not _decide("GET", "/api/reservations/", {"X-Internal-Key": INTERNAL + "x"})


def test_an_unset_key_never_matches(monkeypatch, keys):
    monkeypatch.setattr(app_settings, "internal_api_key", "", raising=False)
    assert not _decide("GET", "/api/reservations/", {"X-Internal-Key": ""})


def test_the_homepage_key_opens_only_the_homepage_paths(keys):
    hp = {"X-Homepage-Key": HOMEPAGE}
    assert _decide("GET", "/api/public/menus", hp)
    assert _decide("GET", "/api/public/slots", hp)
    assert _decide("GET", "/api/public/business-hours", hp)
    assert _decide("POST", "/api/web_reserve", hp)
    assert not _decide("GET", "/api/reservations/", hp)
    assert not _decide("GET", "/api/settings/", hp)
    assert not _decide("POST", "/api/patients/", hp)


def test_the_rpa_key_opens_only_the_three_rpa_paths(keys):
    rpa = {"Authorization": f"Bearer {RPA}"}
    assert _decide("GET", "/api/hotpepper/pending-sync", rpa)
    assert _decide("GET", "/api/hotpepper/pending-sync/", rpa)
    assert _decide("GET", "/api/hotpepper/reconcile-queue", rpa)
    assert _decide("POST", "/api/hotpepper/2582/mark-synced", rpa)
    assert _decide("POST", "/api/hotpepper/2582-2/mark-synced", rpa)
    assert not _decide("POST", "/api/hotpepper/receive-email", rpa)
    assert not _decide("POST", "/api/hotpepper/trigger-poll", rpa)
    assert not _decide("GET", "/api/reservations/", rpa)
    # 種類が違う口の合言葉では開かない
    assert not _decide("GET", "/api/hotpepper/pending-sync", {"X-Homepage-Key": HOMEPAGE})
    assert not _decide("GET", "/api/public/menus", rpa)


def test_the_daily_report_key_opens_only_reading_the_reservation_list(keys):
    # 日計表のツールは config.yaml の auth_token を Bearer で送り、予約の一覧を日付で読む
    daily = {"Authorization": f"Bearer {DAILY}"}
    assert _decide("GET", "/api/reservations/", daily)
    assert _decide("GET", "/api/reservations", daily)
    assert not _decide("POST", "/api/reservations/", daily)
    assert not _decide("GET", "/api/reservations/12", daily)
    assert not _decide("PUT", "/api/reservations/12", daily)
    assert not _decide("GET", "/api/patients/", daily)
    assert not _decide("GET", "/api/hotpepper/pending-sync", daily)
    # RPA の合言葉では予約の一覧は開かない（上のテスト）。逆も同じ
    assert not _decide("GET", "/api/reservations/", {"Authorization": f"Bearer {RPA}"})


# ─── 強さの3段階（ASGI のミドルウェアとして） ───


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok"})


def _client(mode: str, monkeypatch) -> TestClient:
    monkeypatch.setattr(app_settings, "api_access_mode", mode, raising=False)
    return TestClient(ApiAccessMiddleware(_ok_app))


def test_enforce_rejects_without_a_key(keys, monkeypatch):
    client = _client("enforce", monkeypatch)
    response = client.get("/api/reservations/")
    assert response.status_code == 401
    assert client.get("/api/reservations/", headers={"X-Internal-Key": INTERNAL}).status_code == 200
    assert client.get("/health").status_code == 200


def test_report_lets_it_through_and_logs_the_path_only(keys, monkeypatch, caplog):
    client = _client("report", monkeypatch)
    with caplog.at_level(logging.WARNING, logger="app.middlewares.api_access"):
        response = client.get("/api/web_reserve?phone=09000000000")
    assert response.status_code == 200
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "/api/web_reserve" in logged
    # 記録には URL の後ろ（電話番号など）を残さない
    assert "09000000000" not in logged


def test_open_is_the_default_and_changes_nothing(keys, monkeypatch):
    assert type(app_settings).model_fields["api_access_mode"].default == "open"
    client = _client("open", monkeypatch)
    assert client.get("/api/reservations/").status_code == 200


# ─── 本物のアプリに入っていること ───


def test_the_app_is_guarded(keys, monkeypatch):
    from app.main import app

    monkeypatch.setattr(app_settings, "api_access_mode", "enforce", raising=False)
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers={"X-Internal-Key": INTERNAL}).status_code == 200
    assert client.get("/api/auth/me", headers={"X-Homepage-Key": HOMEPAGE}).status_code == 401
    assert client.get("/openapi.json").status_code == 401


def test_api_docs_are_off_in_production():
    from app.main import docs_urls

    assert docs_urls("production") == {"docs_url": None, "redoc_url": None, "openapi_url": None}
    assert docs_urls("development")["openapi_url"] == "/openapi.json"
