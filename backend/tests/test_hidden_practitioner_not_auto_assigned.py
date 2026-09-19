"""非表示の施術者には、自動で予約を割り当てない（2026-09-19）。

非表示の施術者はタイムテーブルに列が出ない。土曜に勤務チェックが入っていたバイトの施術者に、
指名なしのホットペッパー予約とホームページ予約が自動で割り当てられ、誰の画面にも出なかった。
指名なしのホットペッパー予約は「施術者 → 院長」の順で空いている人に入れるので、
非表示の施術者が院長より先に選ばれていた。

本物の PostgreSQL で確かめる（tests/test_hotpepper_rpa_queues_real_db.py と同じ隔離 schema）。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

import app.services.hotpepper_mail as hotpepper_mail
from app.models.practitioner import Practitioner
from app.utils.datetime_jst import now_jst
from tests.test_hotpepper_rpa_queues_real_db import isolated_sessions


async def _seed(sessions) -> dict[str, int]:
    async with sessions() as db:
        staff = {
            "director": Practitioner(name="院長先生", role="院長", display_order=0),
            "hidden": Practitioner(name="バイト", role="施術者", display_order=3, is_visible=False),
        }
        db.add_all(staff.values())
        await db.commit()
        return {key: p.id for key, p in staff.items()}


@pytest.fixture
def everyone_is_free(monkeypatch):
    """勤務と空きの判定は全員「空いている」にして、誰が候補に上がるかだけを見る。"""

    async def always_available(db, practitioner_id, start_time, end_time):
        return True

    monkeypatch.setattr(hotpepper_mail, "_is_practitioner_available", always_available)


@pytest.mark.asyncio
async def test_a_hotpepper_booking_without_designation_skips_a_hidden_practitioner(everyone_is_free):
    start = now_jst() + timedelta(days=1)
    async with isolated_sessions() as sessions:
        ids = await _seed(sessions)
        async with sessions() as db:
            practitioner_id, _ = await hotpepper_mail._assign_practitioner(
                db, None, start, start + timedelta(hours=1)
            )

    assert practitioner_id == ids["director"]


@pytest.mark.asyncio
async def test_a_hotpepper_booking_naming_a_hidden_practitioner_goes_to_a_visible_one(everyone_is_free):
    start = now_jst() + timedelta(days=1)
    async with isolated_sessions() as sessions:
        ids = await _seed(sessions)
        async with sessions() as db:
            practitioner_id, note = await hotpepper_mail._assign_practitioner(
                db, "バイト", start, start + timedelta(hours=1)
            )

    assert practitioner_id == ids["director"]
    assert "見つからず" in (note or "")


@pytest.mark.asyncio
async def test_showing_the_practitioner_makes_them_assignable_again(everyone_is_free):
    """表示に切り替えた時点で、自動の割り当ての対象に戻る。"""
    start = now_jst() + timedelta(days=1)
    async with isolated_sessions() as sessions:
        ids = await _seed(sessions)
        async with sessions() as db:
            hidden = await db.get(Practitioner, ids["hidden"])
            hidden.is_visible = True
            await db.commit()
        async with sessions() as db:
            practitioner_id, _ = await hotpepper_mail._assign_practitioner(
                db, None, start, start + timedelta(hours=1)
            )

    assert practitioner_id == ids["hidden"]
