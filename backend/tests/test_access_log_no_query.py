"""uvicorn の通り道の記録（アクセスログ）に、URL の ? より後ろを残さない。

画面の患者の検索は `/api/patients/search?q=<名前の一部>` を呼ぶ。uvicorn は
アクセスログに ? より後ろまで書くので、患者の名前の一部が本番のログに残っていた
（2026-11-09 に本番のログで確認）。入口の鍵の記録（api_access）と同じく、
パスだけを残す。

本物の uvicorn を起動して1回呼び、記録された1行を確かめる（uvicorn の書き方が
変わっても気づけるように、ログの呼び出しを真似しない）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging

import httpx
import pytest
import uvicorn

ACCESS_LOGGER = "uvicorn.access"
NAME_PART = "たまも"


async def _tiny_app(scope, receive, send):
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok"})


async def _call_once(path: str, http: str) -> None:
    config = uvicorn.Config(
        _tiny_app,
        host="127.0.0.1",
        port=0,
        http=http,
        lifespan="off",
        log_config=None,
        access_log=True,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started, "uvicorn が起動しなかった"
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://127.0.0.1:{port}{path}")
        assert response.status_code == 200
    finally:
        server.should_exit = True
        await task


def _access_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == ACCESS_LOGGER]


_HTTP_IMPLS = ["h11"] + (["httptools"] if importlib.util.find_spec("httptools") else [])


@pytest.fixture
def app_logging():
    """本物のアプリを読み込む（アクセスログの設定は app.main を読み込んだときに入る）。"""
    import app.main  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("http", _HTTP_IMPLS)
async def test_patient_search_words_are_not_written_to_the_access_log(app_logging, caplog, http):
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER)

    await _call_once(f"/api/patients/search?q={NAME_PART}&phone=09000000000", http)

    lines = _access_lines(caplog)
    assert len(lines) == 1, lines
    line = lines[0]
    # パス・方法・結果は今までどおり残る（RPA や日計表が通ったかをログで確かめるため）
    assert '"GET /api/patients/search HTTP/1.1" 200' in line
    # ? より後ろは残さない
    assert "?" not in line
    assert "q=" not in line
    assert NAME_PART not in line
    assert "%E3" not in line.upper()
    assert "09000000000" not in line


@pytest.mark.asyncio
async def test_a_path_without_a_query_is_written_as_before(app_logging, caplog):
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER)

    await _call_once("/health", "h11")

    lines = _access_lines(caplog)
    assert len(lines) == 1, lines
    assert '"GET /health HTTP/1.1" 200' in lines[0]


def test_other_lines_on_the_access_logger_are_kept(app_logging, caplog):
    """形の違う行も消さずにそのまま通す（記録を落とさない）。"""
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER)
    logger = logging.getLogger(ACCESS_LOGGER)

    logger.info("plain message")
    logger.info("%s and %s", "a?b", "c")

    assert _access_lines(caplog) == ["plain message", "a?b and c"]
